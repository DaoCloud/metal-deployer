"""testkit status / logs / cancel: read local results and act on runs."""

import glob
import os
import sys

from testkit.items import ITEMS
from testkit.state import HostLocks, StateStore, pid_alive, read_json, write_json

STATUS_ORDER = ["pass", "fail", "error", "timeout", "stopped", "blocked", "cancelled"]


def list_runs(results_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(results_dir, "*", "run.json"))):
        rec = read_json(path)
        if rec:
            if rec.get("status") == "running" and not pid_alive(rec.get("pid")):
                rec["status"] = "aborted"
            out.append(rec)
    return out


def status(tests, cluster, host=None, out=print):
    state = StateStore(tests.results_dir).load()
    runs = list_runs(tests.results_dir)
    active = [r for r in runs if r.get("status") == "running"]
    for r in active:
        out("running: {} (pid {}, started {})".format(r["run_id"], r["pid"], r["started_at"]))
    hosts = state.get("hosts", {})
    if host:
        if host not in hosts:
            out("{}: no results yet".format(host))
            return 0
        for item in ITEMS:
            e = hosts[host].get(item.id)
            if e:
                out("{:<24} {:<9} run {}  group {}  {}".format(item.id, e["status"], e.get("run_id"),
                                                               e.get("group"), e.get("reason", "")))
        return 0
    if not hosts:
        out("no results yet in {}".format(tests.results_dir))
        return 0
    head = "{:<24}".format("ITEM") + "".join("{:>9}".format(s.upper()) for s in STATUS_ORDER) + "   LAST RUN"
    out(head)
    for item in ITEMS:
        counts = {s: 0 for s in STATUS_ORDER}
        last = ""
        for h in cluster.hosts:
            e = hosts.get(h.name, {}).get(item.id)
            if e:
                counts[e["status"]] = counts.get(e["status"], 0) + 1
                last = max(last, e.get("run_id", ""))
        if any(counts.values()):
            out("{:<24}".format(item.id) + "".join("{:>9}".format(counts.get(s, 0) or "-") for s in STATUS_ORDER)
                + "   " + last)
    failed = sorted((h, i, e) for h, items in hosts.items() for i, e in items.items()
                    if e["status"] in ("fail", "error", "timeout") and not e.get("group_result"))
    if failed:
        out("")
        out("failed hosts:")
        for h, i, e in failed:
            out("  {:<20} {:<24} {:<8} {}".format(h, i, e["status"], e.get("reason", "")))
    return 0


def logs(tests, run_id, host=None, item=None, follow=False, ops=None, out=print):
    run_dir = os.path.join(tests.results_dir, run_id)
    rec = read_json(os.path.join(run_dir, "run.json"))
    if not rec:
        out("error: run {} not found under {}".format(run_id, tests.results_dir))
        return 1
    if not host:
        out("run {}  status {}  started {}  finished {}".format(run_id, rec.get("status"), rec.get("started_at"),
                                                               rec.get("finished_at", "-")))
        for item_id, entry in rec.get("items", {}).items():
            if item and item_id != item:
                continue
            out("{}: {}".format(item_id, entry.get("status")))
            for gid, g in (entry.get("groups") or {}).items():
                out("  [{}] {}  {}s  {}".format(gid, g["status"], g.get("seconds", "-"), g.get("reason", "")))
                for h in g.get("hosts", []):
                    st = g.get("host_status", {}).get(h, "?")
                    if st != "pass":
                        out("     {:<20} {}".format(h, st))
        out("logs of one host: testkit logs {} --host <name> [--item <item>] [--follow]".format(run_id))
        return 0
    items = [i for i in rec.get("items", {}) if not item or i == item]
    if follow:
        if len(items) != 1:
            out("error: --follow needs --item when the run has several items")
            return 1
        from testkit.runners import container_name
        cluster_host = ops["cluster"].by_name.get(host) if ops else None
        if cluster_host is None:
            out("error: unknown host {}".format(host))
            return 1
        return ops["docker"].follow_logs(cluster_host, container_name(run_id, items[0], host))
    shown = False
    for item_id in items:
        base = os.path.join(run_dir, host, item_id)
        for name in ("launcher.log", "container.log"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                out("==> {} <==".format(path))
                with open(path, encoding="utf-8", errors="replace") as f:
                    sys.stdout.write(f.read())
                shown = True
    if not shown:
        out("no logs for {} in run {} (yet)".format(host, run_id))
    return 0


def cancel(tests, cluster, run_id, docker=None, out=print):
    run_dir = os.path.join(tests.results_dir, run_id)
    rec = read_json(os.path.join(run_dir, "run.json"))
    if not rec:
        out("error: run {} not found".format(run_id))
        return 1
    if rec.get("status") not in ("running",):
        out("run {} is not running (status {})".format(run_id, rec.get("status")))
        return 0
    open(os.path.join(run_dir, "CANCEL"), "w").close()
    if pid_alive(rec.get("pid")):
        out("cancel requested; run {} (pid {}) stops its containers within a few seconds".format(run_id, rec["pid"]))
        return 0
    # The testkit process is gone: clean up its containers and locks directly.
    hosts = set()
    for entry in rec.get("items", {}).values():
        hosts.update(entry.get("hosts", []))
    for name in sorted(hosts):
        h = cluster.by_name.get(name)
        if h is not None and docker is not None:
            res = docker.remove_by_label(h, "testkit.run=" + run_id)
            out("{}: {}".format(name, "containers removed" if res.ok else res.message()))
    locks = HostLocks(tests.results_dir)
    names = [os.path.basename(p)[:-len(".lock")] for p in glob.glob(os.path.join(locks.dir, "*.lock"))]
    locks.release(names, run_id=run_id)
    rec["status"] = "cancelled"
    write_json(os.path.join(run_dir, "run.json"), rec)
    out("run {} marked cancelled".format(run_id))
    return 0
