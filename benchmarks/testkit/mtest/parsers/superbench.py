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


def nccl(metrics):
    """Group tables/charts from NCCL size sweeps: busbw of every operation vs message size."""
    series = {}
    for name, v in metrics.items():
        base, _ = split_rank(name)
        m = NCCL_RE.match(base)
        if m and m.group("kind") == "busbw":
            series.setdefault(m.group("bench"), {})[int(m.group("size"))] = v
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
            rows.append([b, top, round(pts[top], 2)])
    table = {"title": "NCCL busbw（最大消息）", "head": ["测试", "消息大小 (B)", "busbw GB/s"], "rows": rows}
    return [table], charts


def parse_group(local_dir, mapping, own, cfg=None):
    """(per_host, group_extra) for one SuperBench run.

    mapping: inventory name / address -> host name. own: results are per host (single-node configs).
    """
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
            entry.update({"status": "fail" if bad else "pass", "reason": "; ".join(bad), "own": True})
            if bad:
                failed.append(h)
        elif bad:
            failed.append("{}: {}".format(h, "; ".join(bad)))
        per_host[h] = entry
    extra = {}
    if not own:
        merged = {}
        for metrics in by_host.values():
            merged.update(metrics)
        tables, charts = nccl(merged)
        extra.update({"tables": tables, "charts": charts})
        r = rdma(merged, mapping)
        if r:
            extra["rdma"] = r
            n = len(r["pairs"])
            extra["summary"] = "{} 节点；ib-traffic {} 个节点对".format(len(hosts), n)
        if failed:
            extra.update({"status": "fail", "reason": "benchmark failed: " + "; ".join(failed)[:400]})
    return per_host, extra
