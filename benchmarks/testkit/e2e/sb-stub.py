#!/usr/bin/env python3
"""Stub of the SuperBench CLI for the mtest end-to-end test (see sbcli-stub.Dockerfile)."""
import json
import os
import subprocess
import sys


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def args_after(name):
    """All values after an option with nargs='+' (until the next option)."""
    a = sys.argv
    if name not in a:
        return []
    out = []
    for v in a[a.index(name) + 1:]:
        if v.startswith("--"):
            break
        out.append(v)
    return out


def ssh(key, user, addr, command):
    p = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
                        "{}@{}".format(user, addr), command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode()


def pairing(hosts, key, user, overrides):
    """Like SuperBench: hostfile = sorted short OS hostnames; the pairing file must be on every host."""
    paths = {o.split("=", 1)[1] for o in overrides if ".parameters.config=" in o}
    if not paths:
        return None
    path = paths.pop()
    texts, names = set(), []
    for _, addr in hosts:
        rc, out = ssh(key, user, addr, "hostname; cat {}".format(path))
        if rc != 0:
            print("pairing file {} missing on {}: {}".format(path, addr, out))
            sys.exit(1)
        name, _, text = out.partition("\n")
        names.append(name.strip().split(".")[0])
        texts.add(text)
    if len(texts) != 1:
        print("pairing file differs between hosts")
        sys.exit(1)
    order = sorted(names, key=lambda n: n.lower())
    rounds = [[tuple(int(x) for x in p.split(",")) for p in line.split(";")] for line in texts.pop().split()]
    return [[(order[a], order[b]) for a, b in r] for r in rounds]


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
    overrides = args_after("--config-override")
    override = next((o for o in overrides if o.startswith("superbench.enable=")), "")
    with open(os.path.join(out, "override.txt"), "w") as f:
        f.write("\n".join(overrides) + "\n")
    lines = []
    config = arg("--config-file")
    if "sharp" in config or "nvl_domain" in config or "all_nodes" in config:
        names = [n for n, _ in hosts]
        enable = override.split("=", 1)[1].strip("[]").replace('"', "").split(",") if override else []
        if "nvl_domain" in config:
            enable = ["nccl-bw:bw:allreduce:mnnvl"]
        if "all_nodes" in config and not enable:
            enable = ["nccl-bw:bw:allreduce:mnnvl-ib"]
        d = {"node": names[0]}
        for i, bench in enumerate(enable):
            d[bench + "/return_code"] = 0
            op = bench.split(":")[2]
            for size in (8, 1048576, 8589934592):
                d["{}/{}_{}_busbw".format(bench, op, size)] = (100.0 + 50 * (i % 2)) * (1 if size < 2 ** 30 else 8)
                d["{}/{}_{}_time".format(bench, op, size)] = 25.0 if size == 8 else 1000.0
            node_dir = os.path.join(out, "nodes", names[0], "benchmarks", bench, "rank0")
            os.makedirs(node_dir, exist_ok=True)
            with open(os.path.join(node_dir, "results.json"), "w") as f:
                json.dump({"raw_data": {"raw_output_0": [
                    "host:1:2 [0] NCCL INFO {} uses Algo {}".format(bench, "NVLS" if bench.endswith("O-B") else "Ring"),
                    "host:1:2 [0] NCCL INFO NET/IB : Using [0]mlx5_0:1/IB [1]mlx5_1:1/IB ; OOB eth0:10.0.0.1<0>",
                    "host:1:3 [0] NCCL INFO Channel 00/0 : 0[0] -> 4[0] [send] via NET/IB/0/GDRDMA"]}}, f)
        lines = [d] + [{"node": n} for n in names[1:]]
    elif "single" in config:
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
        _, key, user = inventory(arg("-f"))
        rounds = pairing(hosts, key, user, overrides)
        if rounds is None:
            rounds = [[(names[a], names[b])] for a in range(len(names)) for b in range(a + 1, len(names))]
        d = {"node": names[0], "ib-traffic:perftest:multinode/return_code": 0}
        for rnd, pairs in enumerate(rounds):
            for idx, (x, y) in enumerate(pairs):
                for rail in range(4):
                    d["ib-traffic:perftest:multinode/ib_write_bw_8388608_gpu-to-gpu_{}_{}:{}_{}:{}".format(
                        rnd, idx, x, y, rail)] = 97.0 + rail
                    d["ib-traffic:perftest:multinode-lat/ib_write_lat_2_cpu-to-cpu_{}_{}:{}_{}:{}".format(
                        rnd, idx, x, y, rail)] = 2.0
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
