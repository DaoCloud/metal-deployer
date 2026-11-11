"""Turn a planned group into a job: which containers to start, and what to launch.

Modes:
  per-node  one container per host, each runs its own command and exits
  mpi       one sshd container per host; rank 0 then runs a launcher (mpirun) inside its container
  delegate  one container on the group master that fans out by itself (SuperBench)
"""

import os
import re
import shlex
from functools import partial

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
                 host_pre=None, host_post=None, parse=None, parse_group=None, host_images=None, timeout=None,
                 group_pre=None, host_collect=None):
        self.mode = mode
        self.group_pre = group_pre          # (shell, timeout) run on the rank 0 host after every host_pre succeeded
        self.host_collect = host_collect or []  # (file, shell): run on every host after group_pre, saved to raw/
        self.parse_group = parse_group      # parse_group(local_dir_of_master) -> ({host: fields}, group_extra)
        self.host_images = host_images or []  # images every host of the group needs (besides its containers)
        self.timeout = timeout              # overrides runtime.run_timeout
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
    if item.id == "imex":
        return imex_job(ctx, item, cfg, nodes, image)
    if item.section == "sharp":
        return sharp_job(ctx, item, cfg, nodes, image)
    if item.section == "superbench" and item.id != "superbench.disk":
        return superbench_job(ctx, item, cfg, nodes, image)

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


def inventory(nodes, ssh_cfg, key_path):
    """Ansible inventory for sb: name = mgmt IP (MPI between workers), ansible_host = LOM (ssh)."""
    lines = ["[all]"]
    for n in nodes:
        lines.append("{} ansible_host={} mtest_name={}".format(n.host.mgmt_ip or n.host.ssh, n.host.ssh, n.host.name))
    lines += ["", "[all:vars]", "ansible_user={}".format(ssh_cfg.get("user", "root")),
              "ansible_port={}".format(ssh_cfg.get("port", 22)), "ansible_ssh_private_key_file={}".format(key_path),
              "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'", ""]
    return "\n".join(lines)


def superbench_job(ctx, item, cfg, nodes, image, worker=None, parser=None):
    """S6: sbcli container on the rank 0 host runs sb deploy/run against the whole group.

    A key made for this run is added to every host's authorized_keys before and removed after.
    """
    from mtest.parsers import superbench
    tests, run_id = ctx.tests, ctx.run_id
    priv, pub, _ = ctx.ssh_keys()
    master = nodes[0]
    tool_dir = node_dir(tests, run_id, item.id, master.host.name) + ".mtest"
    files = [(master.host, tool_dir + "/inventory", inventory(nodes, ctx.cluster.ssh, "/mtest/id_ed25519"), "644"),
             (master.host, tool_dir + "/id_ed25519", priv, "600"),
             (master.host, tool_dir + "/sb_run.sh", node_script("sb_run.sh"), "755")]
    worker = worker or tests.images.get("sb_worker") or ""
    bench = cfg.get("benchmarks") or {}
    env = {"MTEST_SB_CONFIG": cfg.get("config", ""), "MTEST_SB_WORKER": worker,
           "MTEST_SB_RULE": cfg.get("rule", ""), "MTEST_SB_INCLUDE": ",".join(bench.get("include") or []),
           "MTEST_SB_EXCLUDE": ",".join(bench.get("exclude") or [])}
    if cfg.get("config_dir"):
        env["MTEST_SB_CONFIG_DIR"] = cfg["config_dir"]
    spec = _spec(tests, run_id, item.id, master, image, ["/mtest/sb_run.sh"], env,
                 mounts=[(tool_dir, "/mtest", True)])
    spec.extra_args = ["--entrypoint", "bash"]
    pub = pub.strip()
    tag = pub.split()[-1]
    pre = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
           "(grep -qF {k} ~/.ssh/authorized_keys || echo {k} >> ~/.ssh/authorized_keys)").format(k=shlex.quote(pub))
    if not re.match(r"^[A-Za-z0-9._-]+$", tag):
        raise NotReady("unexpected ssh key comment {!r}".format(tag))
    post = "sed -i {} ~/.ssh/authorized_keys; rm -f {}/id_ed25519; true".format(
        shlex.quote("/ {}$/d".format(tag.replace(".", "\\."))), shlex.quote(tool_dir))
    mapping = {}
    for n in nodes:
        for key in (n.host.mgmt_ip, n.host.ssh, n.host.name):
            if key:
                mapping[key] = n.host.name
    own = item.id == "superbench.single_node"
    if parser is None:
        def parser(local_dir):
            return superbench.parse_group(local_dir, mapping, own, cfg)
    else:
        parser = partial(parser, mapping=mapping, cfg=cfg)
    return Job("delegate", [spec], files=files, group_result=True, host_pre=pre, host_post=post,
               host_images=[worker] if worker else [], timeout=cfg.get("timeout"), parse_group=parser)


SHARP_CASES = {"sharp.scale_up": ["U-A", "U-B"], "sharp.scale_out": ["O-A", "O-B", "O-C", "O-D"]}
SHARP_OPS = ["allreduce", "reducescatter", "allgather"]


def sharp_job(ctx, item, cfg, nodes, image):
    """S7a: SuperBench nccl-bw:sharp:<op>:<case> from config_gb300_sharp.yaml, one case per NCCL setting."""
    from mtest.parsers import sharp
    cfg = dict(cfg)
    cfg.setdefault("config", "config_gb300_sharp.yaml")
    cases = cfg.get("cases") or SHARP_CASES[item.id]
    ops = cfg.get("ops") or SHARP_OPS
    cfg["benchmarks"] = {"include": ["nccl-bw:sharp:{}:{}".format(op, c) for c in cases for op in ops], "exclude": []}
    return superbench_job(ctx, item, cfg, nodes, image, worker=cfg.get("worker_image") or None,
                          parser=sharp.parse_group)


def _mpi_job(ctx, item, nodes, image, launcher, tools=None, launcher_env=None):
    """tools: {file name: text} written to every host and mounted read-only at /mtest."""
    tests, run_id = ctx.tests, ctx.run_id
    keys = ctx.ssh_keys()
    files = _mpi_files(tests, run_id, item.id, nodes, keys)
    port = str(tests.runtime["mpi_ssh_port"])
    containers = []
    for n in nodes:
        base = node_dir(tests, run_id, item.id, n.host.name)
        mounts = [(base + ".ssh", SSH_DIR, True)]
        if tools:
            mounts.append((base + ".mtest", "/mtest", True))
            for name, text in tools.items():
                files.append((n.host, "{}.mtest/{}".format(base, name), text, "755" if name.endswith(".sh") else "644"))
        containers.append(_spec(tests, run_id, item.id, n, image, ["sh", "-c", SSHD_ENTRY],
                                {"MTEST_SSH_PORT": port}, mounts=mounts))
    env = dict(containers[0].env)
    env.update({"OMPI_MCA_plm_rsh_args": "-F {}/config".format(SSH_DIR), "OMPI_ALLOW_RUN_AS_ROOT": "1",
                "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1"})
    env.update(launcher_env or {})
    return Job("mpi", containers, launcher=launcher, launcher_env=env, files=files, group_result=True)


def imex_source():
    """imex_fabric_ipc_test.c (node_scripts/ links to build-iso/scripts/; packaging copies the file)."""
    try:
        return node_script("imex_fabric_ipc_test.c")
    except OSError:
        raise NotReady("imex_fabric_ipc_test.c not found next to the node scripts")


def imex_job(ctx, item, cfg, nodes, image):
    """S7: configure IMEX on the hosts, wait for the domain, check, then T1/T2 in mpi-mode containers."""
    from mtest.parsers import imex
    tests, run_id = ctx.tests, ctx.run_id
    scripts = cfg.get("scripts_dir") or "/opt/resource/scripts"
    configure = cfg.get("configure", True)
    wait = int(cfg.get("wait_ready_seconds", 300))
    # Same path and byte-identical node list on every host (IMEX requires identical nodes_config.cfg).
    share = "{}/{}/{}.imex".format(tests.runtime["node_results_dir"].rstrip("/"), run_id, item.id)
    group_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", nodes[0].group.id)
    share += "-" + group_tag
    node_list = "".join(n.host.mgmt_ip + "\n" for n in nodes)
    pre = None
    collect = []
    files = [(n.host, share + "/nodes.list", node_list, "644") for n in nodes]
    if configure:
        pre = ("{s}/imex-configure.sh --nodes {d}/nodes.list --expect {n} > {d}/configure.log 2>&1; rc=$?; "
               "tail -n 3 {d}/configure.log; exit $rc").format(s=shlex.quote(scripts), d=shlex.quote(share),
                                                                 n=len(nodes))
        collect.append(("imex-configure.txt", "cat {}/configure.log; echo IMEX-CONFIGURE-OK".format(shlex.quote(share))))
    collect.append(("imex-check.txt", "{}/imex-check.sh --quiet --level 3 2>&1; true".format(shlex.quote(scripts))))
    group_pre = ("end=$(( $(date +%s) + {w} )); while :; do out=$(timeout 30 nvidia-imex-ctl -N -H 2>&1); "
                 "echo \"$out\" | grep -q 'Domain State: *UP' && break; "
                 "if [ $(date +%s) -ge $end ]; then echo \"$out\"; echo 'IMEX domain not UP after {w}s'; exit 1; fi; "
                 "sleep 5; done; echo \"$out\"").format(w=wait)
    bench = cfg.get("bench") or {}
    env = {"MTEST_IMEX_T1": "1" if bench.get("t1_fabric_ipc", True) else "0",
           "MTEST_IMEX_T2": "1" if bench.get("t2_nvbandwidth", True) else "0",
           "MTEST_IMEX_GPUS": str(nodes[0].host.gpus), "MTEST_NNODES": str(len(nodes))}
    if bench.get("nvbandwidth"):
        env["MTEST_NVBANDWIDTH"] = bench["nvbandwidth"]
    job = _mpi_job(ctx, item, nodes, image, "sh /mtest/imex_bench.sh",
                   tools={"imex_bench.sh": node_script("imex_bench.sh"), "imex_fabric_ipc_test.c": imex_source()},
                   launcher_env=env)
    job.files += files
    job.host_pre = pre
    job.group_pre = (group_pre, wait + 60)
    job.host_collect = collect
    host_dirs = {n.host.name: ctx.local_dir(n.host.name, item.id) for n in nodes}
    job.parse_group = lambda local_dir: imex.parse_group(local_dir, host_dirs, configure, len(nodes))
    return job


def peers_ready_script(nodes, timeout):
    """Shell run in the rank 0 container: wait until sshd answers on every peer."""
    ips = " ".join(shlex.quote(n.host.mgmt_ip) for n in nodes)
    return ("deadline=$(( $(date +%s) + {t} )); for ip in {ips}; do "
            "until ssh -F {d}/config -o ConnectTimeout=5 \"$ip\" true 2>/dev/null; do "
            "[ $(date +%s) -lt $deadline ] || {{ echo \"peer $ip: sshd not reachable\"; exit 1; }}; sleep 2; "
            "done; done").format(t=int(timeout), ips=ips, d=SSH_DIR)
