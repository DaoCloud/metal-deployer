"""Minimal SSH helpers. The executor (P2) builds on these."""

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor


def ssh_base(ssh_cfg, host):
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout={}".format(ssh_cfg.get("connect_timeout", 10)),
            "-o", "StrictHostKeyChecking=accept-new", "-p", str(ssh_cfg.get("port", 22))]
    key = ssh_cfg.get("key")
    if key:
        args += ["-i", os.path.expanduser(str(key))]
    for opt in ssh_cfg.get("options") or []:
        args += ["-o", str(opt)]
    args.append("{}@{}".format(ssh_cfg.get("user", "root"), host.ssh))
    return args


def check_reachable(ssh_cfg, hosts):
    """Run 'true' on every host; returns {name: (ok, message)}."""
    if not shutil.which("ssh"):
        return {h.name: (False, "ssh client not found") for h in hosts}
    timeout = int(ssh_cfg.get("connect_timeout", 10)) + 5

    def one(host):
        try:
            p = subprocess.run(ssh_base(ssh_cfg, host) + ["true"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return host.name, (False, "timeout after {}s".format(timeout))
        if p.returncode == 0:
            return host.name, (True, "ok")
        msg = p.stderr.decode(errors="replace").strip().splitlines()
        return host.name, (False, msg[-1] if msg else "exit {}".format(p.returncode))

    with ThreadPoolExecutor(max_workers=max(1, int(ssh_cfg.get("parallel", 24)))) as pool:
        return dict(pool.map(one, hosts))
