"""testkit report: build one self-contained HTML file (plus CSV) from local results.

Data sources, all under the results directory:
  state.json                                   latest result of every (host, item) and group
  <run>/<host>/<item>/summary.json             metrics and checks of one host
  <run>/<host>/<item>/env.json                 versions reported by the test container
  <run>/<host>/<item>/{container,launcher}.log logs, the tail is embedded for failed hosts
  <run>/_groups/<item>/<group>/summary.json    group status, optional tables / charts / matrix
  nic/<host>.json                              NIC mapping from S3
  <run>/run.json                               run history

Several results directories (testkit report --results A --results B) are merged into one
report: for every (host, item) and (item, group) the result with the newest run id wins.

summary.json metric format: {"name": {"value": 1.0, "unit": "GB/s", "category": "GPU-MEM",
"lower_better": false}}; checks: {"name": {"result": "pass", "reason": ""}}.
"""

import csv
import io
import json
import os
import tarfile
import time
from datetime import datetime

from testkit import __version__, platforms, scaffold
from testkit.items import ITEMS, matches
from testkit.runs import list_runs
from testkit.state import StateStore, load_nic, read_json

OWN_FAILED = ("fail", "error", "timeout")
LOG_TAIL_LINES = 60
# Report order of acceptance layers; "" holds the preparation items (nic, smoke).
LAYERS = ("", "L0", "L1", "L2", "L3", "L4", "L5")
LAYER_NAMES = {"": "准备", "L0": "L0 链路", "L1": "L1 单机", "L2": "L2 RDMA", "L3": "L3 NVLink", "L4": "L4 NCCL",
               "L5": "L5 MoE"}
UNFINISHED = ("stopped", "cancelled", "blocked", "aborted", "running")
VERDICT_RANK = {"skip": 0, "pass": 1, "incomplete": 2, "fail": 3}


def _minutes(start, end):
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        sec = (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()
        return round(sec / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _tail(path, lines=LOG_TAIL_LINES):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def overall(items):
    """Host verdict from its own results: fail / incomplete / pass / none.

    A failed group result (mpi, delegate, or a group stopped by another host) is reported
    on the group and does not change the host verdict.
    """
    if not items:
        return "none"
    own = [e for e in items.values() if not e.get("group_result")]
    if any(e["status"] in OWN_FAILED for e in own):
        return "fail"
    if any(e["status"] in ("stopped", "cancelled", "blocked") for e in items.values()):
        return "incomplete"
    return "pass"


class Columns:
    """Ordered column definitions of one item, collected from host summaries."""

    def __init__(self):
        self.cols = {}

    def add_metric(self, name, m):
        if name in self.cols or (isinstance(m, dict) and m.get("hidden")):
            return
        value = m.get("value") if isinstance(m, dict) else m
        self.cols[name] = {
            "id": name, "name": (m.get("name") if isinstance(m, dict) else None) or name,
            "type": "num" if isinstance(value, (int, float)) and not isinstance(value, bool) else "text",
            "unit": (m.get("unit") or "") if isinstance(m, dict) else "",
            "cat": (m.get("category") or "") if isinstance(m, dict) else "",
            "lower_better": bool(m.get("lower_better")) if isinstance(m, dict) else False,
        }

    def add_check(self, name, c):
        key = "check:" + name
        if key not in self.cols:
            self.cols[key] = {"id": key, "name": (c.get("name") if isinstance(c, dict) else None) or name,
                              "type": "check", "cat": "检查项"}

    def ordered(self):
        cols = list(self.cols.values())
        cats = []
        for c in cols:
            if c["cat"] not in cats:
                cats.append(c["cat"])
        return sorted(cols, key=lambda c: cats.index(c["cat"]))


def merge_states(roots):
    """state.json of every results directory merged: the newest run id wins; each entry gets its "root"."""
    host_state, group_state = {}, {}

    def newer(old, new):
        return old is None or (new.get("run_id") or "") >= (old.get("run_id") or "")

    for root in roots:
        state = StateStore(root).load()
        for host, entries in state.get("hosts", {}).items():
            merged = host_state.setdefault(host, {})
            for item_id, e in entries.items():
                if newer(merged.get(item_id), e):
                    merged[item_id] = dict(e, root=root)
        for item_id, groups in state.get("groups", {}).items():
            merged = group_state.setdefault(item_id, {})
            for gid, g in groups.items():
                if newer(merged.get(gid), g):
                    merged[gid] = dict(g, root=root)
    return host_state, group_state


def _verdict(statuses):
    """One item from the statuses of its hosts and groups: fail / incomplete / pass / skip / none."""
    statuses = [s for s in statuses if s]
    if not statuses:
        return "none"
    if any(s in OWN_FAILED for s in statuses):
        return "fail"
    if any(s in UNFINISHED for s in statuses):
        return "incomplete"
    return "skip" if all(s == "skip" for s in statuses) else "pass"


def build_layers(tests, items, hosts, groups, runs):
    """Acceptance verdict: one row per layer, the overall result and where a gate stopped the last run."""
    from testkit.checks import enabled_items
    enabled = {item.id for item, _ in enabled_items(tests)}
    by_id = {it["id"]: it for it in items}
    for it in items:
        if it.get("view"):
            continue
        own = [g["status"] for g in groups if g["item"] == it["id"]]
        own += [h["items"][it["id"]]["status"] for h in hosts if it["id"] in h["items"]]
        it["status"] = _verdict(own)
        if it["kind"] == "group":
            it["bad"] = sum(1 for g in groups if g["item"] == it["id"] and g["status"] in OWN_FAILED)
        else:
            it["bad"] = sum(1 for h in hosts if h["items"].get(it["id"], {}).get("status") in OWN_FAILED)
    layers = []
    for layer in LAYERS:
        rows = []
        for item in ITEMS:
            if item.layer != layer:
                continue
            it = by_id.get(item.id)
            rows.append({"id": item.id, "title": item.title, "enabled": item.id in enabled, "ran": it is not None,
                         "status": it["status"] if it else "none", "bad": it["bad"] if it else 0})
        ran = [r for r in rows if r["ran"]]
        expected = [r for r in rows if r["ran"] or r["enabled"]]
        if not ran:
            status = "none"
        else:
            status = max((r["status"] for r in ran), key=lambda s: VERDICT_RANK.get(s, 2))
            if len(ran) < len(expected) and VERDICT_RANK.get(status, 2) < VERDICT_RANK["incomplete"]:
                status = "incomplete"
        layers.append({"id": layer, "name": LAYER_NAMES[layer], "status": status, "ran": len(ran),
                       "expected": len(expected), "bad": sum(r["bad"] for r in ran), "items": rows})
    statuses = [l["status"] for l in layers if l["ran"] or l["expected"]]
    if "fail" in statuses:
        overall_status = "fail"
    elif not any(l["ran"] for l in layers):
        overall_status = "none"
    elif any(s in ("incomplete", "none") for s in statuses):
        overall_status = "incomplete"
    else:
        overall_status = "pass"
    gate = None
    last = max(runs, key=lambda r: r["run_id"]) if runs else None
    if last and last.get("gated_at"):
        g = last["gated_at"]
        title = next((i.title for i in ITEMS if i.id == g.get("item")), g.get("item"))
        gate = {"run_id": last["run_id"], "item": g.get("item"), "title": title, "status": g.get("status")}
    return layers, {"status": overall_status, "gate": gate}


def build_data(cluster, tests, title=None, results_dirs=None):
    roots = list(results_dirs or [tests.results_dir])
    host_state, group_state = merge_states(roots)
    ran = {i for entries in host_state.values() for i in entries} | set(group_state)

    hosts = []
    columns = {}
    for h in cluster.hosts:
        entries = {}
        metrics = {}
        logs = {}
        versions = {}
        for item in ITEMS:
            e = host_state.get(h.name, {}).get(item.id)
            if e is None:
                if item.id in ran and any(matches(item.id, s) for s in h.skip):
                    entries[item.id] = {"status": "skip", "reason": "tests.yaml hosts skip", "group_result": True}
                continue
            base = os.path.join(e["root"], e.get("run_id", ""), h.name, item.id)
            summary = read_json(os.path.join(base, "summary.json")) or {}
            entries[item.id] = {
                "status": e["status"], "reason": e.get("reason", ""), "run_id": e.get("run_id"),
                "group": e.get("group"), "rank": e.get("rank"), "group_result": bool(e.get("group_result")),
                "minutes": _minutes(summary.get("started_at"), summary.get("finished_at")),
                "image": summary.get("image", ""), "image_digest": summary.get("image_digest", ""),
            }
            cols = columns.setdefault(item.id, Columns())
            vals = {}
            for name, m in (summary.get("metrics") or {}).items():
                cols.add_metric(name, m)
                vals[name] = m.get("value") if isinstance(m, dict) else m
            for name, c in (summary.get("checks") or {}).items():
                cols.add_check(name, c)
                vals["check:" + name] = c.get("result") if isinstance(c, dict) else c
            if vals:
                metrics[item.id] = vals
            if e["status"] not in ("pass", "skip") and not e.get("group_result"):
                tail = _tail(os.path.join(base, "launcher.log")) or _tail(os.path.join(base, "container.log"))
                if tail:
                    logs[item.id] = tail
            env = read_json(os.path.join(base, "env.json"))
            if isinstance(env, dict):
                versions.update({str(k): str(v) for k, v in env.items() if not isinstance(v, (dict, list))})
        nic = next((n for n in (load_nic(r, h.name) for r in reversed(roots)) if n), None) or {}
        hosts.append({
            "name": h.name, "rack": h.rack, "tray": h.tray, "serial": h.serial or nic.get("serial") or "", "ssh": h.ssh,
            "mgmt": str(h.mgmt) if h.mgmt else "", "storage": str(h.storage) if h.storage else "",
            "ib": [str(i) for i in h.ib if i], "tags": h.tags, "golden": h.golden,
            "items": entries, "metrics": metrics, "logs": logs, "versions": versions,
            "overall": overall(entries), "nic_map": nic.get("rails") or [], "bf3": nic.get("bf3") or {},
        })

    groups = []
    for item in ITEMS:
        for gid, g in (group_state.get(item.id) or {}).items():
            path = os.path.join(g["root"], g.get("run_id", ""), "_groups", item.id, gid, "summary.json")
            full = read_json(path) or {k: v for k, v in g.items() if k != "root"}
            members = full.get("hosts", [])
            groups.append({
                "item": item.id, "id": gid, "run_id": full.get("run_id"), "hosts": members,
                "status": full.get("status"), "reason": full.get("reason", ""), "mode": full.get("mode", ""),
                "first_failed": full.get("first_failed"), "seconds": full.get("seconds"),
                "summary": full.get("summary") or "{} 节点 · {}".format(len(members), full.get("mode", "")),
                "tables": full.get("tables") or [], "charts": full.get("charts") or [],
                "matrix": full.get("matrix"), "log": full.get("log_tail") or "",
            })

    items = []
    for item in ITEMS:
        has_hosts = any(item.id in h["items"] and h["items"][item.id]["status"] != "skip" for h in hosts)
        item_groups = [g for g in groups if g["item"] == item.id]
        if not has_hosts and not item_groups:
            continue
        single = all(len(g["hosts"]) == 1 for g in item_groups)
        items.append({"id": item.id, "title": item.title, "layer": item.layer, "kind": "node" if single else "group",
                      "cols": columns[item.id].ordered() if item.id in columns else []})

    runs, records = [], []
    for root in roots:
        for r in list_runs(root):
            records.append(r)
            members = set()
            for entry in r.get("items", {}).values():
                members.update(entry.get("hosts", []))
            runs.append({"run_id": r["run_id"], "status": r.get("status"), "start": r.get("started_at", ""),
                         "minutes": _minutes(r.get("started_at"), r.get("finished_at")),
                         "items": list(r.get("items", {})), "hosts": len(members), "root": root,
                         "gated_at": r.get("gated_at"),
                         "path": (r["run_id"] if len(roots) == 1 else os.path.join(root, r["run_id"])) + "/"})
    runs.sort(key=lambda r: r["run_id"])

    rdma = build_rdma(cluster, roots[0], group_state)
    if rdma:
        items.append({"id": "rdma", "title": "RDMA 打流汇总（ib-traffic：柜内、跨柜）", "layer": "L2", "kind": "group",
                      "view": "rdma", "cols": []})
    # Stable sort: layer order first, ITEMS order inside a layer, so the RDMA summary ends the L2 items.
    items.sort(key=lambda it: LAYERS.index(it["layer"]) if it["layer"] in LAYERS else len(LAYERS))

    layers, verdict = build_layers(tests, items, hosts, groups, runs)
    starts = [r["start"] for r in runs if r["start"]]
    ends = [r.get("finished_at") for r in records if r.get("finished_at")]
    golden = [h.name for h in cluster.hosts if h.golden]
    report_cfg = tests.data.get("report") or {}
    return {
        "meta": {
            "title": title or report_cfg.get("title") or "{} 测试报告".format((cluster.platform or "").upper()),
            "platform": cluster.platform or "",
            "cluster": "{} 台 / {} rack".format(len(cluster.hosts), len(cluster.racks)),
            "golden": golden[0] if golden else "",
            "start": min(starts) if starts else "", "end": max(ends) if ends else "",
            "tool": "testkit " + __version__,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results_dir": "、".join(roots), "results_dirs": roots,
        },
        "layers": layers, "verdict": verdict,
        "racks": cluster.racks, "trays": max([h.tray for h in cluster.hosts] or [1]),
        "items": items, "hosts": hosts, "groups": groups, "runs": runs, "rdma": rdma,
    }


RDMA_ITEMS = ("superbench.pair", "superbench.rack", "superbench.cross_rack", "superbench.all")


def build_rdma(cluster, results, group_state):
    """Pair x rail matrices for the report from every multi-node SuperBench item that has ib-traffic
    (stage A inside racks, stage B rack to rack, ...); rounds of later items follow those of earlier ones."""
    parts = []
    run_id, used = "", []
    for item_id in RDMA_ITEMS:
        found = False
        for gid, g in (group_state.get(item_id) or {}).items():
            root = g.get("root", results)
            full = read_json(os.path.join(root, g.get("run_id", ""), "_groups", item_id, gid, "summary.json")) or {}
            if full.get("rdma"):
                parts.append(full["rdma"])
                run_id = max(run_id, full.get("run_id", ""))
                found = True
        if found:
            used.append(item_id)
    if parts:
        names = {n for p in parts for key in p["pairs"] for n in key.split("|")}
        order = [h.name for h in cluster.hosts if h.name in names] + sorted(names - set(cluster.by_name))
        idx = {n: i for i, n in enumerate(order)}
        racks = [cluster.by_name[n].rack if n in cluster.by_name else "?" for n in order]
        pairs, keys = [], []
        offset = 0
        for p in parts:
            top = 0
            for key, rnd in sorted(p["pairs"].items(), key=lambda kv: (kv[1], kv[0])):
                a, b = key.split("|")
                pairs.append([idx[a], idx[b], rnd + offset])
                keys.append((p, key))
                top = max(top, rnd + 1)
            offset += top
        tests, values = {}, {}
        for p in parts:
            for tid, t in p["tests"].items():
                tests.setdefault(tid, {k: t[k] for k in ("id", "name", "unit", "cmd", "lower_better")})
        for tid in tests:
            values[tid] = [[(p["tests"].get(tid, {}).get("values", {}).get(key, {}).get(str(rail),
                             p["tests"].get(tid, {}).get("values", {}).get(key, {}).get(rail, -1)))
                            for p, key in keys] for rail in range(platforms.rails(cluster.network))]
        order_ids = sorted(tests, key=lambda t: (t.startswith("lat"), t.startswith("bidir"), t))
        return {"hosts": order, "racks": racks, "pairs": pairs, "rounds": len({p[2] for p in pairs}),
                "tests": [tests[t] for t in order_ids], "values": values, "run_id": run_id,
                "item": ", ".join(used)}
    return None


def render_html(data):
    with open(os.path.join(scaffold.TEMPLATE_DIR, "report.html"), encoding="utf-8") as f:
        template = f.read()
    # "</" inside the JSON would end the <script> element early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = data["meta"]["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return template.replace("__TITLE__", title).replace("__DATA__", payload)


def hosts_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf)
    ids = [i["id"] for i in data["items"]]
    w.writerow(["hostname", "rack", "tray", "serial", "overall"] + ids + ["fail_reason"])
    for h in data["hosts"]:
        reasons = ["{}: {}".format(i, e["reason"]) for i, e in h["items"].items()
                   if e["status"] in OWN_FAILED and not e.get("group_result")]
        w.writerow([h["name"], h["rack"], h["tray"], h["serial"], h["overall"]]
                   + [h["items"].get(i, {}).get("status", "") for i in ids] + ["; ".join(reasons)])
    return buf.getvalue()


def metrics_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run_id", "item", "group", "hostname", "metric", "value", "unit"])
    units = {(i["id"], c["id"]): c.get("unit", "") for i in data["items"] for c in i["cols"]}
    for h in data["hosts"]:
        for item_id, vals in h["metrics"].items():
            e = h["items"].get(item_id, {})
            for name, value in vals.items():
                w.writerow([e.get("run_id", ""), item_id, e.get("group", ""), h["name"], name, value,
                            units.get((item_id, name), "")])
    return buf.getvalue()


def write_report(cluster, tests, out_dir=None, title=None, archive=False, results_dirs=None):
    """Write report.html, hosts.csv, metrics.csv (and raw-logs.tar.gz); returns the written paths."""
    data = build_data(cluster, tests, title, results_dirs)
    out_dir = out_dir or os.path.join(tests.results_dir, "report")
    os.makedirs(out_dir, exist_ok=True)
    formats = (tests.data.get("report") or {}).get("formats") or ["html", "csv"]
    written = []
    if "html" in formats:
        path = os.path.join(out_dir, "report.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_html(data))
        written.append(path)
    if "csv" in formats:
        for name, text in (("hosts.csv", hosts_csv(data)), ("metrics.csv", metrics_csv(data))):
            path = os.path.join(out_dir, name)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(text)
            written.append(path)
    if archive:
        path = os.path.join(out_dir, "raw-logs.tar.gz")
        skip = os.path.realpath(out_dir)
        with tarfile.open(path, "w:gz") as tar:
            roots = data["meta"]["results_dirs"]
            for run in data["runs"]:
                src = os.path.join(run["root"], run["run_id"])
                # With several results directories, runs go under results<N>/ so equal run ids do not clash.
                arc = run["run_id"] if len(roots) == 1 else os.path.join(
                    "results{}".format(roots.index(run["root"]) + 1), run["run_id"])
                if os.path.realpath(src) != skip:
                    tar.add(src, arcname=arc)
        written.append(path)
    return data, written
