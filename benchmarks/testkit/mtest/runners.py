"""Turn a planned group into a job: which containers to start, and what to launch.

Modes:
  per-node  one container per host, each runs its own command and exits
  mpi       one sshd container per host; rank 0 then runs a launcher (mpirun) inside its container
  delegate  one container on the group master that fans out by itself (SuperBench)
"""

import os
import shlex

from mtest.render import HOSTFILE, render

SSH_DIR = os.path.dirname(HOSTFILE)

SMOKE_SCRIPT = r"""
set -u
trap 'echo "smoke: stopped"; exit 143' TERM INT
start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "smoke: host ${MTEST_HOSTNAME} rank ${MTEST_NODE_RANK}/${MTEST_NNODES} group ${MTEST_GROUP}"
gpus=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)
rc="${MTEST_SMOKE_EXIT:-0}"
# A failing host fails at once; a passing host "works" for MTEST_SMOKE_SLEEP seconds.
if [ "$rc" = 0 ]; then
    sleep "${MTEST_SMOKE_SLEEP:-0}" &
    wait $!
fi
status=pass
[ "$rc" = 0 ] || status=fail
cat > /results/summary.json <<EOF
{"item": "smoke", "hostname": "${MTEST_HOSTNAME}", "status": "$status", "started_at": "$start",
 "metrics": {"gpus": {"value": ${gpus:-0}, "unit": ""}},
 "checks": {"container_started": {"result": "pass"}}}
EOF
echo "smoke: exit $rc"
exit "$rc"
"""

SMOKE_MPI_LAUNCHER = (
    "mpirun --allow-run-as-root --hostfile {hostfile} --map-by node -np {nnodes} hostname "
    "> /results/mpi-hostnames.txt 2>&1; rc=$?; cat /results/mpi-hostnames.txt; "
    "n=$(sort -u /results/mpi-hostnames.txt | wc -l); "
    "if [ $rc = 0 ] && [ $n = {nnodes} ]; then s=pass; else s=fail; rc=1; fi; "
    "printf '{\"item\": \"smoke\", \"status\": \"%s\", "
    "\"metrics\": {\"hosts_answered\": {\"value\": %s, \"unit\": \"\"}}}\\n' $s $n "
    "> /results/summary.json; exit $rc"
)

# sshd in the foreground keeps an mpi-mode container alive; keys and config are mounted at /mtest-ssh.
SSHD_ENTRY = (
    "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e -p \"$MTEST_SSH_PORT\" -h {d}/host_key "
    "-o PermitRootLogin=prohibit-password -o PasswordAuthentication=no -o StrictModes=no "
    "-o AuthorizedKeysFile={d}/authorized_keys".format(d=SSH_DIR)
)


class NotReady(Exception):
    """The item has no runner yet (implemented in a later phase)."""


class ContainerSpec:

    def __init__(self, host, rank, name, image, env, cmd, mounts=None, extra_args=None):
        self.host = host
        self.rank = rank
        self.name = name
        self.image = image
        self.env = env
        self.cmd = cmd
        self.mounts = mounts or []          # (host_path, container_path, read_only)
        self.extra_args = extra_args or []


class Job:

    def __init__(self, mode, containers, launcher=None, launcher_env=None, files=None, group_result=False,
                 host_pre=None, host_post=None, parse=None):
        self.mode = mode
        self.host_pre = host_pre            # shell run on each host after preflight, before containers start
        self.host_post = host_post          # shell run on each host where host_pre ran, always at the end
        self.parse = parse                  # parse(local_dir, host) -> summary fields, after results are copied
        self.containers = containers
        self.launcher = launcher            # mpi: shell command run in the rank 0 container
        self.launcher_env = launcher_env or {}
        self.files = files or []            # (host, path, data, mode) written before start
        self.group_result = group_result    # True when one result covers the whole group


def container_name(run_id, item_id, host_name):
    return "acc-{}-{}-{}".format(run_id, item_id, host_name)


def node_dir(tests, run_id, item_id, host_name):
    return "{}/{}/{}/{}".format(tests.runtime["node_results_dir"].rstrip("/"), run_id, item_id, host_name)


def std_env(run_id, item_id, node):
    g = node.group
    return {
        "MTEST_RUN_ID": run_id, "MTEST_ITEM": item_id, "MTEST_GROUP": g.id,
        "MTEST_NODE_RANK": str(node.rank), "MTEST_NNODES": str(len(g.hosts)),
        "MTEST_HOSTNAME": node.host.name, "MTEST_MASTER_ADDR": g.master.mgmt_ip,
    }


def _spec(tests, run_id, item_id, node, image, cmd, extra_env=None, mounts=None):
    env = dict(node.env)
    env.update(std_env(run_id, item_id, node))
    for key, value in (extra_env or {}).items():
        env.setdefault(key, value)
    return ContainerSpec(node.host, node.rank, container_name(run_id, item_id, node.host.name), image, env, cmd,
                         mounts=mounts)


def _mpi_files(tests, run_id, item_id, nodes, keys):
    """Per-host files for mpi mode: client key, host key, authorized_keys, ssh config, hostfile."""
    port = tests.runtime["mpi_ssh_port"]
    priv, pub, host_priv = keys
    config = ("Host *\n  Port {}\n  User root\n  IdentityFile {}/id_ed25519\n  StrictHostKeyChecking no\n"
              "  UserKnownHostsFile /dev/null\n  LogLevel ERROR\n").format(port, SSH_DIR)
    hostfile = "".join("{} slots={}\n".format(n.host.mgmt_ip, n.host.gpus) for n in nodes)
    files = []
    for n in nodes:
        base = node_dir(tests, run_id, item_id, n.host.name) + ".ssh"
        for name, data in (("id_ed25519", priv), ("authorized_keys", pub), ("host_key", host_priv),
                           ("config", config), ("hostfile", hostfile)):
            files.append((n.host, "{}/{}".format(base, name), data, "600"))
    return files


def build_job(ctx, item, cfg, nodes):
    """nodes: NodePlan list of one group, in rank order."""
    tests, run_id = ctx.tests, ctx.run_id
    image = tests.image_for(item, cfg)
    mode = cfg.get("mode", item.mode) if item.id == "smoke" else item.mode

    if item.id == "smoke":
        extra = {"MTEST_SMOKE_SLEEP": str(cfg.get("sleep", 0)), "MTEST_SMOKE_EXIT": str(cfg.get("exit_code", 0))}
        if mode == "per-node":
            return Job(mode, [_spec(tests, run_id, item.id, n, image, n.cmd or ["sh", "-c", SMOKE_SCRIPT], extra)
                              for n in nodes])
        if mode == "delegate":
            master = nodes[0]
            return Job(mode, [_spec(tests, run_id, item.id, master, image,
                                    master.cmd or ["sh", "-c", SMOKE_SCRIPT], extra)], group_result=True)
        if nodes[0].cmd:
            launcher = " ".join(shlex.quote(c) for c in nodes[0].cmd)
        else:
            launcher = render(SMOKE_MPI_LAUNCHER, nodes[0].variables)
        return _mpi_job(ctx, item, nodes, image, launcher)

    if item.id == "dcgm":
        return dcgm_job(ctx, item, cfg, nodes, image)

    if mode == "per-node" and all(n.cmd for n in nodes):
        return Job(mode, [_spec(tests, run_id, item.id, n, image, n.cmd) for n in nodes])
    raise NotReady("{} is not implemented yet ({})".format(item.id, item.phase))


def node_script(name):
    from mtest.nodecmd import script
    return script(name).decode()


def dcgm_job(ctx, item, cfg, nodes, image):
    """S5: dcgmi diag + nvidia-smi checks in the official DCGM image; parsed by mtest.parsers.dcgm."""
    from mtest.parsers import dcgm
    tests, run_id = ctx.tests, ctx.run_id
    mode = cfg.get("hostengine", "container")
    env = {"MTEST_DCGM_MODE": mode, "MTEST_DCGM_LEVEL": str(cfg.get("level", 3)),
           "MTEST_DCGM_HEALTH": "1" if cfg.get("health", True) else "0"}
    text = node_script("dcgm_run.sh")
    specs, files = [], []
    for n in nodes:
        tool_dir = node_dir(tests, run_id, item.id, n.host.name) + ".mtest"
        files.append((n.host, tool_dir + "/dcgm_run.sh", text, "755"))
        mounts = [(tool_dir, "/mtest", True)]
        if mode == "host":
            mounts.append(("/var/log/nvidia-dcgm", "/host-dcgm-log", True))
        spec = _spec(tests, run_id, item.id, n, image, ["/mtest/dcgm_run.sh"], env, mounts=mounts)
        spec.extra_args = ["--entrypoint", "sh"]
        specs.append(spec)
    flag = "/run/mtest-dcgm-stopped-" + run_id
    pre = post = None
    if mode == "container":
        # Only one hostengine may drive the GPUs: stop the host service, start it again afterwards.
        pre = ("rm -f {f}; for s in nvidia-dcgm dcgm; do if systemctl is-active --quiet $s; then "
               "systemctl stop $s && echo $s >> {f}; fi; done; true").format(f=flag)
        post = "[ -f {f} ] && for s in $(cat {f}); do systemctl start $s; done; rm -f {f}; true".format(f=flag)
    return Job("per-node", specs, files=files, host_pre=pre, host_post=post,
               parse=lambda local_dir, host: dcgm.parse(local_dir, cfg, host))


def _mpi_job(ctx, item, nodes, image, launcher):
    tests, run_id = ctx.tests, ctx.run_id
    keys = ctx.ssh_keys()
    files = _mpi_files(tests, run_id, item.id, nodes, keys)
    port = str(tests.runtime["mpi_ssh_port"])
    containers = []
    for n in nodes:
        ssh_dir = node_dir(tests, run_id, item.id, n.host.name) + ".ssh"
        containers.append(_spec(tests, run_id, item.id, n, image, ["sh", "-c", SSHD_ENTRY],
                                {"MTEST_SSH_PORT": port}, mounts=[(ssh_dir, SSH_DIR, True)]))
    env = dict(containers[0].env)
    env.update({"OMPI_MCA_plm_rsh_args": "-F {}/config".format(SSH_DIR), "OMPI_ALLOW_RUN_AS_ROOT": "1",
                "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1"})
    return Job("mpi", containers, launcher=launcher, launcher_env=env, files=files, group_result=True)


def peers_ready_script(nodes, timeout):
    """Shell run in the rank 0 container: wait until sshd answers on every peer."""
    ips = " ".join(shlex.quote(n.host.mgmt_ip) for n in nodes)
    return ("deadline=$(( $(date +%s) + {t} )); for ip in {ips}; do "
            "until ssh -F {d}/config -o ConnectTimeout=5 \"$ip\" true 2>/dev/null; do "
            "[ $(date +%s) -lt $deadline ] || {{ echo \"peer $ip: sshd not reachable\"; exit 1; }}; sleep 2; "
            "done; done").format(t=int(timeout), ips=ips, d=SSH_DIR)
