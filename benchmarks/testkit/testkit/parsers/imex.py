"""S7 IMEX: parse host checks (imex-check.sh), the domain view (nvidia-imex-ctl -N) and T1/T2 output.

Per host, in <host>/imex/raw/: imex-configure.txt, imex-check.txt (imex-check.sh --quiet summary).
On the rank 0 host also: group-pre.txt (wait for Domain State UP, ends with nvidia-imex-ctl -N -H output),
t1-build.log/rc, t1-pair.log/rc, t1-all.log/rc, t2-nvbandwidth.log/rc or t2-skip.txt.
"""

import os
import re

# imex-check.sh rows about this host only; other L2/L3 rows describe the whole domain.
HOST_LOCAL = ("nvidia-smi Fabric State",)
CHECK_RE = re.compile(r"^(L[0-3]|S)\s+(PASS|FAIL|WARN|SKIP)\s+(.+?)(?:\s{2,}(.*))?$")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _rc(path):
    text = _read(path)
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def parse_check(text):
    """[(level, status, name, detail)] from the imex-check.sh summary block."""
    out = []
    block = (text or "").split("IMEX check summary")[-1]
    for line in block.splitlines():
        m = CHECK_RE.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2), m.group(3).strip(), (m.group(4) or "").strip()))
    return out


def parse_ctl(text):
    """{"nodes": [(idx, addr, state)], "matrix": [[sym]], "domain": str} from nvidia-imex-ctl -N output."""
    nodes, matrix, domain = [], [], ""
    in_matrix = False
    for line in (text or "").splitlines():
        m = re.match(r"^Node #\s*(\d+)\s*-\s*(\S+)\s*-\s*([A-Z_]+)", line.strip())
        if m:
            nodes.append((int(m.group(1)), m.group(2), m.group(3)))
            continue
        if "Nodes From" in line:
            in_matrix = True
            matrix = []
            continue
        m = re.match(r"^\s*Domain State:\s*(\S+)", line)
        if m:
            domain = m.group(1)
            in_matrix = False
            continue
        if in_matrix:
            parts = line.split()
            if len(parts) > 1 and parts[0].isdigit():
                matrix.append(parts[1:])
    return {"nodes": nodes, "matrix": matrix, "domain": domain}


def parse_host(local_dir, configured):
    raw = os.path.join(local_dir, "raw")
    checks, problems = {}, []
    if configured:
        conf = _read(os.path.join(raw, "imex-configure.txt"))
        if conf is not None:
            ok = "IMEX-CONFIGURE-OK" in conf
            tail = [l for l in conf.strip().splitlines() if l.strip()][-1:] or [""]
            checks["imex_configure"] = {"result": "pass" if ok else "fail", "reason": "" if ok else tail[0][:200]}
            if not ok:
                problems.append("imex-configure.sh failed: " + tail[0][:200])
    rows = parse_check(_read(os.path.join(raw, "imex-check.txt")))
    for level, status, name, detail in rows:
        res = {"PASS": "pass", "FAIL": "fail", "WARN": "pass", "SKIP": "skip"}[status]
        checks["{} {}".format(level, name)] = {"result": res, "reason": ("WARN: " if status == "WARN" else "") + detail}
        # L2/L3 describe the whole domain (counted on the group); node-local failures count here.
        if status == "FAIL" and (level in ("L0", "L1") or name in HOST_LOCAL):
            problems.append("{}: {}".format(name, detail))
    if not rows:
        checks["imex_check"] = {"result": "fail", "reason": "no imex-check.sh output"}
        problems.append("no imex-check.sh output")
    return {"status": "fail" if problems else "pass", "reason": "; ".join(problems)[:400], "checks": checks,
            "metrics": {}, "own": True}


def parse_group(master_dir, host_dirs, configured=True, expect_nodes=None):
    """(per_host, group_extra) for one rack."""
    per_host = {h: parse_host(d, configured) for h, d in host_dirs.items()}
    raw = os.path.join(master_dir, "raw")
    ctl = parse_ctl(_read(os.path.join(raw, "group-pre.txt")))
    problems, rows = [], []

    ready = sum(1 for n in ctl["nodes"] if n[2] == "READY")
    total = len(ctl["nodes"])
    want = expect_nodes or len(host_dirs)
    rows.append(["节点 READY", "pass" if total == want and ready == total else "fail", "{}/{}".format(ready, total)])
    cells = [c for r in ctl["matrix"] for c in r]
    bad = sorted({c for c in cells if c != "C"})
    rows.append(["连接矩阵全部 C", "pass" if cells and not bad else "fail",
                 "{}x{}".format(len(ctl["matrix"]), len(ctl["matrix"][0]) if ctl["matrix"] else 0)
                 + ("; 非 C: " + ",".join(bad) if bad else "")])
    rows.append(["Domain State", "pass" if ctl["domain"] == "UP" else "fail", ctl["domain"] or "unknown"])
    # Domain-wide imex-check rows of the rank 0 host (fabric probe, Cluster UUID, Fabric Health, -sanity,
    # ping matrix); READY / matrix / Domain State are already covered from nvidia-imex-ctl -N above.
    covered = ("all nodes READY", "connectivity matrix all C", "domain state")
    for level, status, name, detail in parse_check(_read(os.path.join(raw, "imex-check.txt"))):
        if level in ("L2", "L3") and status == "FAIL" and name not in covered and name not in HOST_LOCAL:
            rows.append(["{} {}".format(level, name), "fail", detail])
    for r in rows:
        if r[1] == "fail":
            problems.append("{}: {}".format(r[0], r[2]))

    t1_build = _rc(os.path.join(raw, "t1-build.rc"))
    if t1_build is not None:
        if t1_build != 0:
            tail = (_read(os.path.join(raw, "t1-build.log")) or "").strip().splitlines()[-1:] or [""]
            rows.append(["T1 编译 imex_fabric_ipc_test", "fail", tail[0][:200]])
            problems.append("T1 build failed")
        for name, label in (("t1-pair", "T1 fabric IPC（2 节点）"), ("t1-all", "T1 fabric IPC（全部节点）")):
            rc = _rc(os.path.join(raw, name + ".rc"))
            if rc is None:
                continue
            log = _read(os.path.join(raw, name + ".log")) or ""
            bw = re.findall(r"write:\s*([0-9.]+)\s*GB/s", log)
            detail = "write {} GB/s".format(bw[0]) if bw else "{} slices PASS".format(
                len(re.findall(r"PASS\s*$", log, re.M)))
            if rc == 2:
                detail = "FABRIC handles unsupported: IMEX down or GPU not in the NVLink domain"
            elif rc != 0:
                detail = "exit {}".format(rc)
            rows.append([label, "pass" if rc == 0 else "fail", detail])
            if rc != 0:
                problems.append("{}: {}".format(label, detail))
    metrics_t2 = []
    rc = _rc(os.path.join(raw, "t2-nvbandwidth.rc"))
    if rc is not None:
        log = _read(os.path.join(raw, "t2-nvbandwidth.log")) or ""
        sums = re.findall(r"^SUM\s+(\S+)\s+([0-9.]+)", log, re.M)
        detail = "; ".join("{} {} GB/s".format(n.replace("multinode_device_to_device_", ""), v) for n, v in sums)
        rows.append(["T2 nvbandwidth multinode", "pass" if rc == 0 else "fail", detail or "exit {}".format(rc)])
        metrics_t2 = sums
        if rc != 0:
            problems.append("T2 nvbandwidth exit {}".format(rc))
    elif _read(os.path.join(raw, "t2-skip.txt")):
        rows.append(["T2 nvbandwidth multinode", "skip", _read(os.path.join(raw, "t2-skip.txt")).strip()])

    labels = [n[1] for n in ctl["nodes"]]
    extra = {"tables": [{"title": "IMEX 域与 T1/T2", "head": ["检查", "结果", "说明"], "rows": rows}],
             "summary": "{} 节点；Domain State {}".format(len(host_dirs), ctl["domain"] or "unknown")}
    if ctl["matrix"]:
        extra["matrix"] = {"labels": ["n{}".format(i) for i in range(len(ctl["matrix"]))],
                           "cells": ctl["matrix"], "addrs": labels}
    if metrics_t2:
        extra["metrics"] = {n: float(v) for n, v in metrics_t2}
    if problems:
        extra.update({"status": "fail", "reason": "; ".join(problems)[:400]})
    return per_host, extra
