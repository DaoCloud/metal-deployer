"""Which network devices NCCL used, from its NCCL_DEBUG=INFO log (SuperBench results.json raw output).

Each line starts with "<host>:<pid>:<tid> [<cuda dev>] NCCL INFO ...". Checked per host:
  - "NET/IB : Using [0]mlx5_0:1/IB [1]...": only InfiniBand devices, and (with S3 data) only CX8 rails;
    a RoCE device is the BF3 (Ethernet) port and must not carry NCCL traffic
  - "Channel ... [send] via NET/IB/<k>" without a proxy: GPU d sends on the rail of GPU d (with PXN the
    proxy "(r)" may use another GPU's rail, which is allowed)
  - "via NET/Socket" or "NET/Socket : Using": NCCL fell back to TCP
"""

import json
import os
import re

PREFIX = re.compile(r"^\s*(?P<host>[^\s:]+):(?P<pid>\d+):\d+ \[(?P<dev>\d+)\] NCCL (?:INFO|WARN) (?P<msg>.*)$")
USING = re.compile(r"NET/IB : Using (?P<list>.*)$")
DEVICE = re.compile(r"\[(?P<k>\d+)\](?P<dev>[\w.-]+):(?P<port>\d+)/(?P<kind>IB|RoCE)")
CHANNEL = re.compile(r"Channel \d+/\d+ : \d+\[(?P<dev>\d+)\] -> \d+\[\d+\] \[(?:send|receive)\] via "
                     r"NET/(?P<net>IB|Socket)/(?P<k>\d+)(?:\((?P<proxy>\d+)\))?")


def _strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)


def log_lines(run_dir, bench_prefix="nccl-bw"):
    """NCCL log lines from every file under run_dir whose path names an nccl-bw benchmark."""
    out = []
    for root, _, files in os.walk(run_dir):
        if bench_prefix not in root:
            continue
        for name in files:
            try:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if name.endswith(".json"):
                found = []
                try:
                    _strings(json.loads(text), found)
                    text = "\n".join(found)
                except ValueError:
                    pass
            out += [l for l in text.splitlines() if " NCCL " in l]
    return out


def rails_by_gpu(nic):
    """{gpu index: ibdev} from an S3 record (testkit nic discover)."""
    out = {}
    for r in (nic or {}).get("rails") or []:
        if isinstance(r, dict) and r.get("ibdev") and r.get("gpu") is not None:
            out[int(r["gpu"])] = r["ibdev"]
        elif isinstance(r, (list, tuple)) and len(r) >= 3 and r[2] and r[1] is not None:
            out[int(r[1])] = r[2]
    return out


def check(lines, mapping, rails):
    """({host: [problem]}, rows, seen). mapping: OS hostname -> host; rails: {host: {gpu: ibdev}}."""
    using, problems, rows, seen = {}, {}, set(), False
    channels = []
    for line in lines:
        m = PREFIX.match(line)
        if not m:
            continue
        host = mapping.get(m.group("host"), mapping.get(m.group("host").split(".")[0], m.group("host")))
        msg = m.group("msg")
        u = USING.search(msg)
        if u:
            seen = True
            devs = {int(d.group("k")): (d.group("dev"), d.group("kind")) for d in DEVICE.finditer(u.group("list"))}
            using[(host, m.group("pid"))] = devs
            for k, (dev, kind) in sorted(devs.items()):
                known = rails.get(host)
                if kind != "IB":
                    problems.setdefault(host, []).append("NCCL uses {} ({}), not an IB rail".format(dev, kind))
                elif known and dev not in known.values():
                    problems.setdefault(host, []).append("NCCL uses {}, not a CX8 rail".format(dev))
            continue
        if "NET/Socket : Using" in msg:
            seen = True
            problems.setdefault(host, []).append("NCCL fell back to NET/Socket (TCP)")
            continue
        c = CHANNEL.search(msg)
        if c:
            seen = True
            channels.append((host, m.group("pid"), int(c.group("dev")), c.group("net"), int(c.group("k")),
                             c.group("proxy")))
    for host, pid, dev, net, k, proxy in channels:
        if net == "Socket":
            problems.setdefault(host, []).append("GPU{} channel via NET/Socket".format(dev))
            continue
        nic = (using.get((host, pid)) or {}).get(k, ("?", ""))[0]
        want = (rails.get(host) or {}).get(dev)
        if proxy is not None:
            rows.add((host, dev, nic, want or "—", "PXN"))
            continue
        ok = want is None or nic == want
        rows.add((host, dev, nic, want or "—", "pass" if ok else "fail"))
        if not ok:
            problems.setdefault(host, []).append("GPU{} sends on {}, its rail is {}".format(dev, nic, want))
    for host in problems:
        problems[host] = sorted(set(problems[host]))
    return problems, sorted(rows), seen
