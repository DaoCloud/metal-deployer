"""S7a IB SHARP: SuperBench nccl-bw:sharp:<op>:<case> results -> per-case busbw, comparisons, NCCL log evidence.

Comparisons are shown, not judged (no baselines): O-B/O-A, O-D/O-B, O-D/O-C, O-C/O-A at the largest
message size. A case fails only when its benchmark fails (return code), e.g. when a forced NCCL_ALGO
cannot be used.
"""

import json
import os
import re

from mtest.parsers import superbench

BENCH_RE = re.compile(r"^nccl-bw:sharp:(?P<op>[a-z]+):(?P<case>[A-Z]-[A-Z])$")
RATIOS = [("O-B", "O-A"), ("O-D", "O-B"), ("O-D", "O-C"), ("O-C", "O-A")]
EVIDENCE = re.compile(r"NCCL (INFO|WARN).*(NVLS|CollNet|SHARP|Sharp|NET/|plugin|Algo|algorithm)", re.I)


def _strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)


def evidence(run_dir, bench, limit=12):
    """NCCL INFO/WARN lines about NVLS, CollNet/SHARP, the net plugin and algorithms for one benchmark."""
    lines = []
    for root, _, files in os.walk(run_dir):
        if bench not in root:
            continue
        for name in files:
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if name.endswith(".json"):
                try:
                    found = []
                    _strings(json.loads(text), found)
                    text = "\n".join(found)
                except ValueError:
                    pass
            for line in text.splitlines():
                if EVIDENCE.search(line):
                    line = re.sub(r"^\S+:\d+:\d+ ", "", line.strip())
                    if line not in lines:
                        lines.append(line)
    return lines[:limit]


def parse_group(local_dir, mapping, cfg=None, rails=None):
    per_host, extra = superbench.parse_group(local_dir, mapping, own=False, cfg=cfg, rails=rails)
    if extra.get("status") == "error" and not per_host:
        return per_host, extra
    data = superbench.read_summary(os.path.join(local_dir, "sb", "run", "results-summary.jsonl"))
    merged = {}
    for metrics in data.values():
        merged.update(metrics)
    curves, rcs = {}, {}
    for name, v in merged.items():
        base, _ = superbench.split_rank(name)
        bench, _, metric = base.partition("/")
        m = BENCH_RE.match(bench)
        if not m:
            continue
        op, case = m.group("op"), m.group("case")
        if metric == "return_code":
            rcs[(op, case)] = v if rcs.get((op, case), 0) == 0 else rcs[(op, case)]
            continue
        mm = re.match(r"^[a-z]+_(\d+)_busbw$", metric)
        if mm:
            curves.setdefault(op, {}).setdefault(case, {})[int(mm.group(1))] = v
    ops = sorted({op for op, _ in rcs} | set(curves))
    cases = sorted({c for _, c in rcs} | {c for o in curves.values() for c in o})
    charts, rows, ratio_rows = [], [], []
    for op in ops:
        sizes = sorted({s for c in curves.get(op, {}).values() for s in c})
        if sizes:
            charts.append({"title": "{} busbw：{}".format(op, " / ".join(cases)), "unit": "GB/s", "x": sizes,
                           "series": [{"name": c, "y": [curves[op][c].get(s, 0) for s in sizes]}
                                      for c in cases if c in curves[op]]})
        top = {}
        for c in cases:
            pts = curves.get(op, {}).get(c) or {}
            rc = rcs.get((op, c))
            big = max(pts) if pts else None
            top[c] = pts[big] if big else None
            rows.append([op, c, "pass" if rc == 0 else ("fail" if rc is not None else "—"),
                         round(top[c], 2) if top[c] is not None else "—", big or "—"])
        for a, b in RATIOS:
            if top.get(a) and top.get(b):
                ratio_rows.append([op, "{} / {}".format(a, b), round(top[a] / top[b], 3)])
    run_dir = os.path.join(local_dir, "sb", "run")
    log = []
    for op in ops:
        for c in cases:
            ev = evidence(run_dir, "nccl-bw:sharp:{}:{}".format(op, c))
            if ev:
                log.append("== {} {}".format(op, c))
                log.extend(ev)
    extra["tables"] = [{"title": "各组 busbw（最大消息）", "head": ["操作", "组", "结果", "busbw GB/s", "消息 (B)"],
                        "rows": rows}]
    if ratio_rows:
        extra["tables"].append({"title": "开 / 关 SHARP 对比（仅展示，不判定）", "head": ["操作", "对比", "倍数"],
                                "rows": ratio_rows})
    extra["charts"] = charts
    extra["log_tail"] = "\n".join(log) if log else "未找到 NCCL 日志（results.json 中没有原始输出）"
    failed = ["{} {}".format(op, c) for (op, c), rc in sorted(rcs.items()) if rc != 0]
    extra["summary"] = "{} 节点；组 {}".format(len(set(mapping.values())), ", ".join(cases) or "—")
    if failed:
        extra.update({"status": "fail", "reason": "failed: " + ", ".join(failed)})
    elif not rcs:
        extra.update({"status": "error", "reason": "no nccl-bw:sharp results"})
    return per_host, extra
