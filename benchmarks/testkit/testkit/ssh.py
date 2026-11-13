"""SSH transport: run commands on hosts, stream output, and copy result directories back."""

import io
import os
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor


# testkit has no ssh settings in its files: passwordless login as root is set up beforehand.
# TESTKIT_SSH_CONFIG names an extra ssh config file ("ssh -F"), e.g. for a key or a ProxyJump.
DEFAULTS = {"user": "root", "port": 22, "parallel": 24, "connect_timeout": 10}


def settings():
    return dict(DEFAULTS, config=os.environ.get("TESTKIT_SSH_CONFIG") or None)


class Result:

    def __init__(self, rc, out="", err=""):
        self.rc = rc
        self.out = out
        self.err = err

    @property
    def ok(self):
        return self.rc == 0

    def message(self):
        lines = (self.err or self.out or "").strip().splitlines()
        return lines[-1] if lines else "exit {}".format(self.rc)


class Remote:
    """Runs shell commands on hosts over ssh, reusing one connection per host (ControlMaster)."""

    def __init__(self, ssh_cfg):
        self.cfg = ssh_cfg
        self.ctl_dir = os.path.join(tempfile.gettempdir(), "testkit-ssh-{}".format(os.getuid()))
        os.makedirs(self.ctl_dir, mode=0o700, exist_ok=True)

    def options(self):
        cfg = self.cfg
        args = ["-F", os.path.expanduser(cfg["config"])] if cfg.get("config") else []
        args += ["-o", "BatchMode=yes", "-o", "ConnectTimeout={}".format(cfg.get("connect_timeout", 10)),
                "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=15",
                "-o", "ControlMaster=auto", "-o", "ControlPath={}/%C".format(self.ctl_dir),
                "-o", "ControlPersist=120", "-p", str(cfg.get("port", 22))]
        key = cfg.get("key")
        if key:
            args += ["-i", os.path.expanduser(str(key))]
        for opt in cfg.get("options") or []:
            args += ["-o", str(opt)]
        return args

    def dest(self, host):
        return "{}@{}".format(self.cfg.get("user", "root"), host.ssh)

    def argv(self, host, command):
        return ["ssh"] + self.options() + [self.dest(host), command]

    def run(self, host, command, timeout=None, input=None):
        """Run a shell command string on the host. rc 124 means the local timeout expired."""
        try:
            p = subprocess.run(self.argv(host, command), input=input,
                               stdin=None if input is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                               start_new_session=True)
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout after {}s: {}".format(timeout, command))
        except OSError as e:
            return Result(127, "", str(e))
        return Result(p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))

    def popen(self, host, command, stdout, stderr=subprocess.STDOUT):
        return subprocess.Popen(self.argv(host, command), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                start_new_session=True)

    def put(self, host, path, data, mode="600", timeout=60):
        if isinstance(data, str):
            data = data.encode()
        cmd = "mkdir -p {d} && cat > {p} && chmod {m} {p}".format(
            d=shlex.quote(os.path.dirname(path)), p=shlex.quote(path), m=mode)
        return self.run(host, cmd, timeout=timeout, input=data)

    def fetch(self, host, remote_dir, local_dir, timeout=600):
        """Copy the contents of remote_dir into local_dir."""
        os.makedirs(local_dir, exist_ok=True)
        if shutil.which("rsync"):
            ssh_cmd = " ".join(shlex.quote(a) for a in ["ssh"] + self.options())
            argv = ["rsync", "-a", "-e", ssh_cmd, "{}:{}/".format(self.dest(host), remote_dir.rstrip("/")),
                    local_dir.rstrip("/") + "/"]
            try:
                p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout, start_new_session=True)
            except subprocess.TimeoutExpired:
                return Result(124, "", "rsync timeout after {}s".format(timeout))
            return Result(p.returncode, "", p.stderr.decode(errors="replace"))
        res = self.run_bytes(host, "tar -C {} -cf - .".format(shlex.quote(remote_dir)), timeout)
        if not res.ok:
            return res
        try:
            with tarfile.open(fileobj=io.BytesIO(res.out), mode="r:") as tar:
                return safe_extract(tar, local_dir)
        except tarfile.TarError as e:
            return Result(1, "", "bad archive from {}: {}".format(host.name, e))

    def run_bytes(self, host, command, timeout=None):
        try:
            p = subprocess.run(self.argv(host, command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout, start_new_session=True)
        except subprocess.TimeoutExpired:
            return Result(124, b"", "timeout after {}s".format(timeout))
        return Result(p.returncode, p.stdout, p.stderr.decode(errors="replace"))


def safe_extract(tar, local_dir):
    """Extract only regular files and directories that stay inside local_dir.

    The archive comes from a test container running as root on a remote host, so links,
    devices and paths leaving local_dir are refused.
    """
    root = os.path.realpath(local_dir)
    members = tar.getmembers()
    for m in members:
        target = os.path.realpath(os.path.join(root, m.name))
        if not (m.isfile() or m.isdir()):
            return Result(1, "", "refused non-regular member in archive: {}".format(m.name))
        if os.path.commonpath([root, target]) != root:
            return Result(1, "", "refused path outside the results directory: {}".format(m.name))
    for m in members:
        target = os.path.realpath(os.path.join(root, m.name))
        if m.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        src = tar.extractfile(m)
        with open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return Result(0)


def check_reachable(ssh_cfg, hosts):
    """Run 'true' on every host; returns {name: (ok, message)}."""
    if not shutil.which("ssh"):
        return {h.name: (False, "ssh client not found") for h in hosts}
    remote = Remote(ssh_cfg)
    timeout = int(ssh_cfg.get("connect_timeout", 10)) + 5

    def one(host):
        res = remote.run(host, "true", timeout=timeout)
        return host.name, (res.ok, "ok" if res.ok else res.message())

    with ThreadPoolExecutor(max_workers=max(1, int(ssh_cfg.get("parallel", 24)))) as pool:
        return dict(pool.map(one, hosts))
