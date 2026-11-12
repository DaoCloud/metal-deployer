#!/usr/bin/env python3
"""Read-only snapshot of every NVLink and IB port of this host: state and error counters.

Run by mtest over ssh (python3 -, arguments in MTEST_ARGS as base64 JSON). Prints one JSON line.
nvidia-smi output is returned as text and parsed on the control node (mtest.parsers.links).
Args: sysfs (test hook, default /sys), smi (nvidia-smi path), timeout (seconds per command),
      since (epoch: kernel messages after it, else the current boot), mlxlink (also run mlxlink per IB port).
"""

import base64
import json
import os
import re
import subprocess
import time


def args():
    raw = os.environ.get("MTEST_ARGS")
    return json.loads(base64.b64decode(raw).decode()) if raw else {}


def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace")
    except FileNotFoundError:
        return 127, "{}: not found".format(cmd[0])
    except subprocess.TimeoutExpired:
        return 124, "{}: timeout after {}s".format(" ".join(cmd), timeout)


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def ib_ports(sysfs):
    """{"mlx5_0/1": {"link_layer", "state", "rate", "counters": {name: int}}} for every RDMA port."""
    out = {}
    base = os.path.join(sysfs, "class", "infiniband")
    try:
        devs = sorted(os.listdir(base))
    except OSError:
        return out
    for dev in devs:
        ports = os.path.join(base, dev, "ports")
        try:
            names = sorted(os.listdir(ports), key=lambda p: int(p) if p.isdigit() else 0)
        except OSError:
            continue
        for port in names:
            pdir = os.path.join(ports, port)
            counters = {}
            cdir = os.path.join(pdir, "counters")
            try:
                for name in sorted(os.listdir(cdir)):
                    value = read(os.path.join(cdir, name))
                    if value is not None and value.lstrip("-").isdigit():
                        counters[name] = int(value)
            except OSError:
                pass
            out["{}/{}".format(dev, port)] = {
                "link_layer": read(os.path.join(pdir, "link_layer")) or "",
                "state": read(os.path.join(pdir, "state")) or "",
                "phys_state": read(os.path.join(pdir, "phys_state")) or "",
                "rate": read(os.path.join(pdir, "rate")) or "",
                "counters": counters,
            }
    return out


def kernel(since, timeout):
    """Kernel messages about GPUs, NVLink, NICs and PCIe: since an epoch time, or the current boot."""
    cmd = ["journalctl", "-k", "--no-pager", "-o", "short-iso"]
    cmd += ["--since", "@{}".format(int(since))] if since else ["-b"]
    rc, text = run(cmd, timeout)
    if rc != 0:
        if since:
            return {"error": text.strip()[-300:], "lines": []}
        rc, text = run(["dmesg"], timeout)
        if rc != 0:
            return {"error": text.strip()[-300:], "lines": []}
    keep = re.compile(r"NVRM|Xid|mlx5|nvswitch|nvidia|AER|fallen off|pcieport|infiniband", re.I)
    lines = [l for l in text.splitlines() if keep.search(l)]
    return {"error": "", "lines": lines[-500:]}


def mlxlink(ports, timeout):
    """{"mlx5_0/1": parsed JSON or {"error": ...}} from mlxlink -m -c -e --json for IB link-layer ports."""
    out = {}
    for key, info in ports.items():
        if info.get("link_layer", "").lower() != "infiniband":
            continue
        dev, port = key.split("/")
        rc, text = run(["mlxlink", "-d", dev, "-p", port, "-m", "-c", "-e", "--json"], timeout)
        start = text.find("{")
        try:
            out[key] = json.loads(text[start:]) if start >= 0 else {"error": text.strip()[-300:]}
        except ValueError:
            out[key] = {"error": "rc {}: {}".format(rc, text.strip()[-300:])}
    return out


def main():
    a = args()
    sysfs = a.get("sysfs") or "/sys"
    smi = a.get("smi") or "nvidia-smi"
    timeout = int(a.get("timeout") or 60)
    out = {"time": time.time(), "smi": {}, "rc": {}}
    for key, cmd in (("gpus", [smi, "--query-gpu=index,pci.bus_id,serial", "--format=csv,noheader"]),
                     ("nvlink_status", [smi, "nvlink", "-s"]),
                     ("nvlink_errors", [smi, "nvlink", "-e"]),
                     ("p2p", [smi, "topo", "-p2p", "n"])):
        if a.get("skip_gpu"):
            continue
        rc, text = run(cmd, timeout)
        out["smi"][key] = text
        out["rc"][key] = rc
    out["ib"] = ib_ports(sysfs)
    out["kernel"] = kernel(a.get("since"), timeout)
    if a.get("mlxlink"):
        out["mlxlink"] = mlxlink(out["ib"], timeout)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
