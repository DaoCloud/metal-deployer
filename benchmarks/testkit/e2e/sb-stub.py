#!/usr/bin/env python3
"""Stub of the SuperBench CLI for the mtest end-to-end test (see sbcli-stub.Dockerfile)."""
import json
import os
import subprocess
import sys


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def inventory(path):
    hosts, key, user = [], None, "root"
    for line in open(path):
        parts = line.split()
        if parts and "ansible_host=" in line and not line.startswith("["):
            hosts.append((parts[0], dict(p.split("=", 1) for p in parts[1:])["ansible_host"]))
        if line.startswith("ansible_ssh_private_key_file="):
            key = line.strip().split("=", 1)[1]
        if line.startswith("ansible_user="):
            user = line.strip().split("=", 1)[1]
    return hosts, key, user


def deploy():
    hosts, key, user = inventory(arg("-f"))
    image = arg("-i")
    for name, addr in hosts:
        p = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "{}@{}".format(user, addr),
                            "docker image inspect {} >/dev/null && echo ok".format(image)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print("deploy {} ({}): {}".format(name, addr, p.stdout.decode().strip()))
        if p.returncode != 0:
            sys.exit(1)
    os.makedirs(arg("-o"), exist_ok=True)


def run():
    hosts, _, _ = inventory(arg("-f"))
    out = arg("--output-dir")
    os.makedirs(out, exist_ok=True)
    override = arg("--config-override", "")
    with open(os.path.join(out, "override.txt"), "w") as f:
        f.write(override + "\n")
    lines = []
    if "single" in arg("--config-file"):
        for i, (name, _) in enumerate(hosts):
            d = {"node": name}
            for r in range(4):
                d["cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops:{}".format(r)] = 4500.0 - 10 * i - r
                d["cublaslt-gemm:compute/return_code:{}".format(r)] = 0
            if not override or "gpu-burn" in override:
                d["gpu-burn/return_code"] = 1 if i == 1 else 0
            lines.append(d)
    else:
        names = [n for n, _ in hosts]
        d = {"node": names[0], "ib-traffic:perftest:multinode/return_code": 0}
        rnd = 0
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                for rail in range(4):
                    d["ib-traffic:perftest:multinode/ib_write_bw_8388608_gpu-to-gpu_{}_0:{}_{}:{}".format(
                        rnd, names[a], names[b], rail)] = 97.0 + rail
                    d["ib-traffic:perftest:multinode-lat/ib_write_lat_2_cpu-to-cpu_{}_0:{}_{}:{}".format(
                        rnd, names[a], names[b], rail)] = 2.0
                rnd += 1
        for size in (1048576, 8589934592):
            d["nccl-bw:bw:allreduce:ib/allreduce_{}_busbw".format(size)] = 10.0 if size < 2 ** 30 else 360.0
        lines = [d] + [{"node": n} for n in names[1:]]
    with open(os.path.join(out, "results-summary.jsonl"), "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    print("sb run: {} nodes".format(len(hosts)))


def result():
    out = arg("--output-dir")
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "results-summary.md"), "w").write("# summary\n")


cmd = sys.argv[1:2]
{"deploy": deploy, "run": run, "result": result}.get(cmd[0] if cmd else "", lambda: sys.exit(2))()
