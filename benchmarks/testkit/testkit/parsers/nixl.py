"""NIXLBench results (node_scripts/nixl_run.sh): raw/<run>.txt, raw/<run>.target.txt and raw/<run>.rc.

<run> is the case name (s1, posix, ...) or <case>@<initiator>+<target> for pair cases. The initiator
prints one table row per block size:
  Block Size (B)  Batch Size  B/W (GB/Sec)  Avg Lat. (us)  Avg Prep (us) ...
A run fails when nixlbench exits non-zero, prints no table, or reports "Consistency check failed".
Optional limits per case (tests.yaml): min_bw_gbps (bandwidth at the largest block) and max_lat_us
(latency at the smallest block); without them results are only recorded.
"""

import os
import re

ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)(\s|$)")
ERROR_RE = re.compile(r"Consistency check failed|Segmentation fault|terminate called|Aborted", re.I)


def parse_rows(text):
    """[(block_bytes, batch, bw_gbps, lat_us)] from the initiator table."""
    rows = []
    for line in (text or "").splitlines():
        m = ROW_RE.match(line)
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))))
    return rows


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _rc(path):
    text = _read(path)
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _limit(cfg, key, case):
    value = (cfg.get(key) or {}).get(case)
    return float(value) if value is not None else None


def parse_group(local_dir, cfg, master, expected, notes=None, storage=False):
    """master: rank 0 host (local and storage runs); expected: run names in order; a run without
    results (nixl_run.sh stopped early) fails. storage: runs have no target process."""
    raw = os.path.join(local_dir, "raw")
    if os.path.exists(os.path.join(raw, "etcd-failed")):
        return {}, {"status": "error", "reason": "etcd did not start in the rank 0 container (raw/etcd.log)"}
    try:
        done = {f[:-3] for f in os.listdir(raw) if f.endswith(".rc")}
    except OSError:
        done = set()
    if not done:
        return {}, {"status": "error", "reason": "no NIXLBench results"}
    runs = list(expected) + sorted(done - set(expected))
    rows, failed, charts, metrics, bad_hosts = [], [], [], {}, {}
    for run in runs:
        case, _, peers = run.partition("@")
        init, _, target = peers.partition("+")
        if run not in done:
            rows.append([case, init or master, target or ("-" if storage else master), "-", "-", "-", "-",
                         "fail: not run"])
            failed.append("{}: not run".format(run))
            for h in (init, target):
                if h:
                    bad_hosts.setdefault(h, []).append("{} not run".format(case))
            continue
        rc = _rc(os.path.join(raw, run + ".rc"))
        out = _read(os.path.join(raw, run + ".txt")) or ""
        tout = _read(os.path.join(raw, run + ".target.txt")) or ""
        table = parse_rows(out)
        problems = []
        if rc != 0:
            problems.append("exit {}".format("timeout" if rc == 124 else rc))
        if not table:
            problems.append("no result table")
        err = ERROR_RE.search(out) or ERROR_RE.search(tout)
        if err:
            problems.append(err.group(0))
        lat = bw = None
        if table:
            first, last = table[0], table[-1]
            lat, bw = first[3], last[2]
            min_bw, max_lat = _limit(cfg, "min_bw_gbps", case), _limit(cfg, "max_lat_us", case)
            if min_bw is not None and bw < min_bw:
                problems.append("{:.2f} GB/s < {:g}".format(bw, min_bw))
            if max_lat is not None and lat > max_lat:
                problems.append("{:.1f} us > {:g}".format(lat, max_lat))
            if len(table) > 1:
                charts.append({"title": "NIXLBench {}".format(run), "unit": "GB/s", "x": [r[0] for r in table],
                               "series": [{"name": "B/W", "y": [r[2] for r in table]}]})
            if not peers:
                metrics["nixl/{}/bw_gbps".format(case)] = {"value": bw, "unit": "GB/s"}
                metrics["nixl/{}/lat_us".format(case)] = {"value": lat, "unit": "us"}
        result = "fail" if problems else "pass"
        rows.append([case, init or master, target or ("-" if storage else master),
                     table[0][0] if table else "-", lat if lat is not None else "-",
                     table[-1][0] if table else "-", round(bw, 3) if bw is not None else "-",
                     result if not problems else "fail: " + "; ".join(problems)])
        if problems:
            failed.append("{}: {}".format(run, "; ".join(problems)))
            for h in (init, target):
                if h:
                    bad_hosts.setdefault(h, []).append("{} {}".format(case, "; ".join(problems)))
    per_host = {}
    if metrics:
        per_host[master] = {"metrics": metrics}
    for h, why in bad_hosts.items():
        per_host.setdefault(h, {}).update({"status": "fail", "reason": "; ".join(why)[:400], "own": True})
    extra = {"tables": [{"title": "NIXLBench（延迟取最小块，带宽取最大块）",
                         "head": ["测试", "initiator", "target", "最小块 (B)", "延迟 (us)", "最大块 (B)",
                                  "带宽 (GB/s)", "结果"], "rows": rows}],
             "charts": charts[:8], "summary": "{} 次运行，{} 失败".format(len(runs), len(failed))}
    if notes:
        extra["notes"] = list(notes)
    if failed:
        extra.update({"status": "fail", "reason": "; ".join(failed)[:400]})
    return per_host, extra
