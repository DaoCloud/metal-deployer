"""Turn SuperBench results (sb run --output-dir: results-summary.jsonl) into mtest summaries.

results-summary.jsonl has one JSON line per node: {"node": <inventory name>, "<benchmark>/<metric>": value}.
Per-GPU ranks add a ":<rank>" suffix; mtest keeps the worst rank. Multi-node benchmarks report on the
rank 0 node only. ib-traffic metrics name the host pair and rail:
  ib-traffic:perftest:multinode-lat/ib_write_lat_2_cpu-to-cpu_<round>_<idx>:<hostA>_<hostB>:<rail>
"""

import json
import os
import re

CATEGORIES = [
    (r"^cublaslt-gemm", "GPU-COMPUTE"), (r"^(nvbandwidth|gpu-copy-bw)", "GPU-MEM"), (r"^gpu-burn", "GPU-STABILITY"),
    (r"^cpu-stream", "CPU-MEM"), (r"^ib-loopback", "RDMA-LOCAL"), (r"^ib-traffic", "RDMA-MULTINODE"),
    (r"^nccl-bw:.*:nvlink", "NCCL-NVLINK"), (r"^nccl-bw:.*:mnnvl", "NCCL-MNNVL"), (r"^nccl-bw:.*:ib", "NCCL-IB"),
    (r"^disk-benchmark", "DISK"),
]
RANK_RE = re.compile(r"^(.*?):(\d+)$")
NCCL_RE = re.compile(r"^(?P<bench>nccl-bw[^/]*)/(?P<op>[a-z]+)_(?P<size>\d+)_(?P<kind>busbw|algbw|time)$")
LOOPBACK_RE = re.compile(r"^(?P<bench>ib-loopback[^/]*)/(?P<pre>.*?)_(?P<size>\d+)(?P<post>.*)$")
IBT_RE = re.compile(r"^(?P<bench>ib-traffic[^/]*)/(?P<cmd>ib_\w+?)_(?P<size>\d+)_(?P<dir>[a-z]+-to-[a-z]+)_"
                    r"(?P<round>\d+)_(?P<idx>\d+):(?P<a>[^_:]+)_(?P<b>[^_:]+):(?P<rail>\d+)$")
DIR_NAME = {"gpu-to-gpu": "gpu→gpu", "cpu-to-cpu": "cpu→cpu", "gpu-to-cpu": "gpu→cpu", "cpu-to-gpu": "cpu→gpu"}


def category(name):
    for pat, cat in CATEGORIES:
        if re.search(pat, name):
            return cat
    return "OTHER"


def lower_better(name):
    n = name.lower()
    return any(k in n for k in ("_lat", "latency", "_time", "t_avg"))


def read_summary(path):
    """{node: {metric: value}} from results-summary.jsonl."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                node = str(d.pop("node", d.pop("hostname", "")))
                out.setdefault(node, {}).update({k: v for k, v in d.items() if isinstance(v, (int, float))})
    except OSError:
        pass
    return out


def split_rank(name):
    if name.startswith("ib-traffic"):
        return name, None
    m = RANK_RE.match(name)
    return (m.group(1), int(m.group(2))) if m and "/" in m.group(1) else (name, None)


def return_codes(metrics):
    """{benchmark: worst return code}"""
    out = {}
    for name, v in metrics.items():
        base, _ = split_rank(name)
        if base.endswith("/return_code"):
            bench = base[:-len("/return_code")]
            out[bench] = v if out.get(bench, 0) == 0 else out[bench]
    return out


def aggregate(metrics):
    """Collapse per-rank metrics to the worst value; mark which ones the report shows."""
    agg = {}
    for name, v in metrics.items():
        if name.startswith(("monitor/", "ib-traffic")) or name.endswith("/return_code") or "/return_code:" in name:
            continue
        base, _ = split_rank(name)
        lb = lower_better(base)
        cur = agg.get(base)
        if cur is None or (v > cur if lb else v < cur):
            agg[base] = v
    # Show only the largest message size of size sweeps (NCCL busbw, ib-loopback).
    largest = {}
    for name in agg:
        m = NCCL_RE.match(name)
        if m:
            key = (m.group("bench"), m.group("op"), m.group("kind"))
        else:
            m = LOOPBACK_RE.match(name)
            key = (m.group("bench"), m.group("pre"), m.group("post")) if m else None
        if key:
            largest[key] = max(largest.get(key, 0), int(m.group("size")))
    out = {}
    for name, v in agg.items():
        hidden = False
        m = NCCL_RE.match(name)
        if m:
            hidden = m.group("kind") != "busbw" or int(m.group("size")) != largest[(m.group("bench"), m.group("op"),
                                                                                    m.group("kind"))]
        else:
            m = LOOPBACK_RE.match(name)
            if m:
                hidden = int(m.group("size")) != largest[(m.group("bench"), m.group("pre"), m.group("post"))]
        out[name] = {"value": round(v, 4) if isinstance(v, float) else v, "unit": "", "category": category(name),
                     "lower_better": lower_better(name), "hidden": hidden}
    return out


def _host_of(name, mapping):
    return mapping.get(name, name)


def rdma(metrics, mapping):
    """RDMA pair/rail values from ib-traffic metrics, in the format the report merges across groups."""
    tests = {}
    pairs = {}
    for name, v in metrics.items():
        m = IBT_RE.match(name)
        if not m:
            continue
        bidir = m.group("bench").endswith("bidir")
        lat = "_lat" in m.group("cmd")
        tid = "{}_{}".format("lat" if lat else ("bidir" if bidir else "bw"), m.group("dir"))
        if tid not in tests:
            d = DIR_NAME.get(m.group("dir"), m.group("dir"))
            tests[tid] = {"id": tid, "unit": "us" if lat else "GB/s", "lower_better": lat,
                          "name": "时延 {}".format(d) if lat else "带宽 {} {}".format(
                              d.replace("→", "↔") if bidir else d, "双向" if bidir else "单向"),
                          "cmd": "{} -s {}{}".format(m.group("cmd"), m.group("size"), " -b" if bidir else ""),
                          "values": {}}
        a, b = _host_of(m.group("a"), mapping), _host_of(m.group("b"), mapping)
        key = "|".join(sorted((a, b)))
        pairs.setdefault(key, int(m.group("round")))
        tests[tid]["values"].setdefault(key, {})[int(m.group("rail"))] = v
    return {"pairs": pairs, "tests": tests} if tests else None


def small_times(metrics, small):
    """{bench: (size_lo, time_lo, size_hi, time_hi)}: NCCL time (us) at the ends of the small-message range."""
    lo, hi = small
    times = {}
    for name, v in metrics.items():
        base, _ = split_rank(name)
        m = NCCL_RE.match(base)
        if m and m.group("kind") == "time":
            size = int(m.group("size"))
            if lo <= size <= hi:
                pts = times.setdefault(m.group("bench"), {})
                pts[size] = max(pts.get(size, v), v)      # worst rank
    out = {}
    for bench, pts in times.items():
        a, b = min(pts), max(pts)
        out[bench] = (a, pts[a], b, pts[b])
    return out


def nccl(metrics, small=(8, 65536)):
    """Group tables/charts from NCCL size sweeps: busbw of every operation vs message size, and the
    time of small messages (latency) at the ends of the small range."""
    series = {}
    for name, v in metrics.items():
        base, _ = split_rank(name)
        m = NCCL_RE.match(base)
        if m and m.group("kind") == "busbw":
            series.setdefault(m.group("bench"), {})[int(m.group("size"))] = v
    lat = small_times(metrics, small)
    if not series:
        return [], []
    sizes = sorted({s for pts in series.values() for s in pts if s >= 1 << 20})
    charts = []
    by_cat = {}
    for bench, pts in sorted(series.items()):
        by_cat.setdefault(category(bench), []).append((bench, pts))
    rows = []
    for cat, items in sorted(by_cat.items()):
        if sizes:
            charts.append({"title": "{} busbw".format(cat), "unit": "GB/s", "x": sizes,
                           "series": [{"name": b.split(":")[2] if b.count(":") >= 2 else b,
                                       "y": [pts.get(s, 0) for s in sizes]} for b, pts in items]})
        for b, pts in items:
            top = max(pts)
            t = lat.get(b)
            rows.append([b, top, round(pts[top], 2), round(t[1], 2) if t else "—", round(t[3], 2) if t else "—"])
    lo, hi = small
    table = {"title": "NCCL busbw（最大消息）与小消息时延", "head": [
        "测试", "消息大小 (B)", "busbw GB/s", "{} B 时延 us".format(lo), "{} B 时延 us".format(hi)], "rows": rows}
    return [table], charts


def parse_group(local_dir, mapping, own, cfg=None, rails=None):
    """(per_host, group_extra) for one SuperBench run.

    mapping: inventory name / address / OS hostname -> host name. own: results are per host (single-node
    configs). cfg: the item config; "outlier" compares hosts (single-node metrics, ib-traffic per rail) with
    the median of the group, "baseline" sets minimum NCCL busbw (GB/s, largest message) per benchmark,
    "hca_check" judges the network devices in the NCCL log. rails: {host: {gpu: ibdev}} from S3.
    """
    cfg = cfg or {}
    small = tuple(cfg.get("small_message_bytes") or (8, 65536))
    data = read_summary(os.path.join(local_dir, "sb", "run", "results-summary.jsonl"))
    by_host = {}
    for node, metrics in data.items():
        by_host.setdefault(_host_of(node, mapping), {}).update(metrics)
    hosts = sorted(set(mapping.values()))
    per_host = {}
    failed = []
    if not data:
        return {}, {"status": "error", "reason": "no SuperBench results (results-summary.jsonl missing or empty)"}
    for h in hosts:
        metrics = by_host.get(h)
        if metrics is None:
            if own:
                per_host[h] = {"status": "error", "reason": "no SuperBench results for this host", "own": True,
                               "metrics": {}, "checks": {}}
                failed.append(h)
            continue
        rcs = return_codes(metrics)
        checks = {"rc:" + b: {"result": "pass" if rc == 0 else "fail", "reason": "return code {}".format(rc)}
                  for b, rc in sorted(rcs.items())}
        bad = ["{} rc {}".format(b, rc) for b, rc in sorted(rcs.items()) if rc != 0]
        entry = {"metrics": aggregate(metrics), "checks": checks}
        if own:
            for bench, (a, ta, b, tb) in small_times(metrics, small).items():
                entry["metrics"]["{}/time_{}B".format(bench, a)] = {"value": round(ta, 2), "unit": "us",
                                                                   "lower_better": True, "category": category(bench),
                                                                   "name": "{} {} B 时延".format(bench, a)}
            entry.update({"status": "fail" if bad else "pass", "reason": "; ".join(bad), "own": True})
            if bad:
                failed.append(h)
        elif bad:
            failed.append("{}: {}".format(h, "; ".join(bad)))
        per_host[h] = entry
    extra = {}
    hca_rows = nccl_hca(local_dir, data, mapping, rails or {}, cfg.get("hca_check", "fail"), per_host, failed, own)
    out_cfg = dict(OUTLIER_DEFAULTS, **(cfg.get("outlier") or {}))
    if own:
        flagged = host_outliers(per_host, out_cfg)
        for h, reasons in flagged.items():
            _flag(per_host[h], "outlier", reasons, out_cfg["action"])
            if out_cfg["action"] == "fail" and h not in failed:
                failed.append(h)
    if not own:
        merged = {}
        for metrics in by_host.values():
            merged.update(metrics)
        tables, charts = nccl(merged, small)
        if hca_rows:
            tables.append({"title": "NCCL 网卡使用（GPU → 网卡，期望为本 rail）",
                           "head": ["主机", "GPU", "使用的网卡", "本 rail 网卡", "结果"], "rows": hca_rows[:300]})
        extra.update({"tables": tables, "charts": charts})
        r = rdma(merged, mapping)
        if r:
            extra["rdma"] = r
            n = len(r["pairs"])
            extra["summary"] = "{} 节点；ib-traffic {} 个节点对".format(len(hosts), n)
            flagged, rail_rows = rail_outliers(r, out_cfg)
            if rail_rows:
                tables.append({"title": "IB rail 离群（与组内中位数相差超过 {}%）".format(out_cfg["pct"]),
                               "head": ["测试", "主机", "rail", "该 rail 中位数", "组内中位数", "偏差 %"],
                               "rows": rail_rows})
            for h, reasons in flagged.items():
                entry = per_host.setdefault(h, {"metrics": {}, "checks": {}})
                _flag(entry, "ib_rail_outlier", reasons, out_cfg["action"])
                if out_cfg["action"] == "fail":
                    failed.append("{}: {}".format(h, "; ".join(reasons)[:200]))
        low = nccl_below_baseline(tables, cfg.get("baseline") or {})
        if low:
            failed.append("NCCL below baseline: " + "; ".join(low))
        if failed:
            extra.update({"status": "fail", "reason": "benchmark failed: " + "; ".join(failed)[:400]})
    return per_host, extra


OUTLIER_DEFAULTS = {"pct": 10, "action": "fail", "metrics": [r"^nvbandwidth/", r"^gpu-copy-bw", r"^ib-loopback"],
                    "rdma_tests": ["bw_gpu-to-gpu", "bidir_gpu-to-gpu"]}


def nccl_hca(local_dir, data, mapping, rails, action, per_host, failed, own):
    """Check the NCCL log of the run (see mtest.parsers.nccl_log); adds the "nccl_hca" check to hosts.
    Returns the table rows [host, GPU, NIC used, rail NIC, result]."""
    from mtest.parsers import nccl_log
    if action == "off" or not any(k.startswith("nccl-bw") for m in data.values() for k in m):
        return []
    lines = nccl_log.log_lines(os.path.join(local_dir, "sb", "run"))
    problems, rows, seen = nccl_log.check(lines, mapping, rails)
    if not seen:
        for entry in per_host.values():
            entry.setdefault("checks", {})["nccl_hca"] = {"result": "skip", "reason": "no NCCL NET log (NCCL_DEBUG=INFO)"}
        return []
    for h, entry in per_host.items():
        if h not in problems:
            entry.setdefault("checks", {})["nccl_hca"] = {"result": "pass", "reason": "every GPU on its own IB rail"}
    for h, msgs in sorted(problems.items()):
        entry = per_host.setdefault(h, {"metrics": {}, "checks": {}})
        _flag(entry, "nccl_hca", msgs, action)
        if action == "fail":
            failed.append(h if own else "{}: {}".format(h, "; ".join(msgs)[:200]))
    return [[h, d, nic, want, res] for h, d, nic, want, res in rows]


def _flag(entry, check, reasons, action):
    """Add a check to a host entry; with action fail the host fails on its own, warn only shows it."""
    entry.setdefault("checks", {})[check] = {"result": "fail" if action == "fail" else "warn",
                                             "reason": "; ".join(reasons)[:400]}
    if action == "fail":
        entry.update({"status": "fail", "own": True,
                      "reason": "; ".join(filter(None, [entry.get("reason", ""), "; ".join(reasons)]))[:400]})


def host_outliers(per_host, cfg):
    """{host: [reason]} for single-node metrics more than pct % worse than the median of all hosts."""
    from mtest.outlier import deviation, outliers
    pats = [re.compile(p) for p in cfg.get("metrics") or []]
    names = sorted({m for e in per_host.values() for m, v in (e.get("metrics") or {}).items()
                    if any(p.search(m) for p in pats) and not (isinstance(v, dict) and v.get("hidden"))})
    out = {}
    for name in names:
        values = {h: (e.get("metrics") or {}).get(name, {}).get("value") for h, e in per_host.items()}
        values = {h: v for h, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        lb = lower_better(name)
        for h, (v, med) in outliers(values, float(cfg["pct"]), lb).items():
            out.setdefault(h, []).append("{} {} vs median {} ({}%)".format(name, v, round(med, 2), deviation(v, med)))
    return out


def rail_outliers(r, cfg):
    """({host: [reason]}, table rows) for (host, rail) whose median over all pairs is an outlier."""
    from mtest.outlier import deviation, median, outliers
    flagged, rows = {}, []
    for tid in cfg.get("rdma_tests") or []:
        test = r["tests"].get(tid)
        if not test:
            continue
        per = {}
        for key, rails in test["values"].items():
            for host in key.split("|"):
                for rail, v in rails.items():
                    if isinstance(v, (int, float)) and v > 0:
                        per.setdefault((host, rail), []).append(v)
        values = {k: median(v) for k, v in per.items()}
        for (host, rail), (v, med) in sorted(outliers(values, float(cfg["pct"]), test["lower_better"]).items()):
            rows.append([test["name"], host, rail, round(v, 2), round(med, 2), deviation(v, med)])
            flagged.setdefault(host, []).append("rail {} {} {} vs median {}".format(rail, test["name"], round(v, 2),
                                                                                  round(med, 2)))
    return flagged, rows


def nccl_below_baseline(tables, baseline):
    """["<bench> busbw X < Y GB/s"] from the NCCL table rows [bench, size, busbw]."""
    out = []
    rows = next((t["rows"] for t in tables if t["title"].startswith("NCCL busbw")), [])
    for bench, minimum in sorted(baseline.items()):
        for row in rows:
            if row[0] == bench and row[2] < float(minimum):
                out.append("{} busbw {} < {} GB/s".format(bench, row[2], minimum))
    return out
