"""NVSHMEM perftest results (node_scripts/nvshmem_run.sh): raw/<test>.txt and raw/<test>.rc.

With NVSHMEM_MACHINE_READABLE_OUTPUT=1 every value is one line:
  &&&& PERF <job>___<subjob>___size__<bytes>___<var> <value> <+|-><unit>
"+" means higher is better (bandwidth), "-" lower is better (latency). Results are recorded, not judged;
only a perftest that exits non-zero fails the group.
"""

import os
import re

PERF_RE = re.compile(r"^&&&& PERF (?P<job>\S+?)___(?P<sub>\S*?)___size__(?P<size>\d+)___(?P<var>\S+) "
                     r"(?P<value>[-+\d.eE]+) (?P<sign>[+-])(?P<unit>\S+)")


def parse_perf(text):
    """[(job, subjob, size, var, value, higher_better, unit)]"""
    out = []
    for line in (text or "").splitlines():
        m = PERF_RE.match(line.strip())
        if m:
            try:
                value = float(m.group("value"))
            except ValueError:
                continue
            out.append((m.group("job"), m.group("sub"), int(m.group("size")), m.group("var"), value,
                        m.group("sign") == "+", m.group("unit")))
    return out


def parse_group(local_dir):
    raw = os.path.join(local_dir, "raw")
    if os.path.exists(os.path.join(raw, "perftest-missing")):
        return {}, {"status": "error", "reason": "NVSHMEM perftests not found in the image (libnvshmem3-dev-cuda-13)"}
    try:
        names = sorted(f[:-3] for f in os.listdir(raw) if f.endswith(".rc"))
    except OSError:
        names = []
    if not names:
        return {}, {"status": "error", "reason": "no NVSHMEM perftest results"}
    rows, failed, charts = [], [], []
    for name in names:
        try:
            with open(os.path.join(raw, name + ".rc"), encoding="utf-8") as f:
                rc = int(f.read().strip() or 1)
        except (OSError, ValueError):
            rc = 1
        try:
            with open(os.path.join(raw, name + ".txt"), encoding="utf-8", errors="replace") as f:
                perf = parse_perf(f.read())
        except OSError:
            perf = []
        if rc != 0 or not perf:
            failed.append("{} exit {}{}".format(name, rc, "" if perf else ", no PERF lines"))
        series = {}
        for job, sub, size, var, value, hb, unit in perf:
            series.setdefault((job, sub, var, hb, unit), {})[size] = value
        for (job, sub, var, hb, unit), pts in sorted(series.items()):
            size = max(pts) if hb else min(pts)
            rows.append([name, sub or "-", var, unit, size, round(pts[size], 3)])
            if len(pts) > 1:
                sizes = sorted(pts)
                charts.append({"title": "{} {} ({})".format(name, sub, var), "unit": unit, "x": sizes,
                               "series": [{"name": var, "y": [pts[s] for s in sizes]}]})
    extra = {"tables": [{"title": "NVSHMEM perftest（带宽取最大消息，延迟取最小消息）",
                         "head": ["测试", "scope", "指标", "单位", "消息大小 (B)", "值"], "rows": rows}],
             "charts": charts[:8], "summary": "{} 个 perftest".format(len(names))}
    if failed:
        extra.update({"status": "fail", "reason": "; ".join(failed)[:400]})
    return {}, extra
