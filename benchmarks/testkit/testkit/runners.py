"""Turn a planned group into a job: which containers to start, and what to launch.

Modes:
  per-node  one container per host, each runs its own command and exits
  mpi       one sshd container per host; rank 0 then runs a launcher (mpirun) inside its container
  delegate  one container on the group master that fans out by itself (SuperBench)
"""

import json
import os
import re
import shlex
import time

from testkit.render import HOSTFILE, render

SSH_DIR = os.path.dirname(HOSTFILE)

SMOKE_SCRIPT = r"""
set -u
trap 'echo "smoke: stopped"; exit 143' TERM INT
start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "smoke: host ${TESTKIT_HOSTNAME} rank ${TESTKIT_NODE_RANK}/${TESTKIT_NNODES} group ${TESTKIT_GROUP}"
gpus=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)
rc="${TESTKIT_SMOKE_EXIT:-0}"
# A failing host fails at once; a passing host "works" for TESTKIT_SMOKE_SLEEP seconds.
if [ "$rc" = 0 ]; then
    sleep "${TESTKIT_SMOKE_SLEEP:-0}" &
    wait $!
fi
status=pass
[ "$rc" = 0 ] || status=fail
cat > /results/summary.json <<EOF
{"item": "smoke", "hostname": "${TESTKIT_HOSTNAME}", "status": "$status", "started_at": "$start",
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

# sshd in the foreground keeps an mpi-mode container alive; keys and config are mounted at /testkit-ssh.
# sshd overwrites /proc/1/environ (process title), so the container env is saved for ssh sessions first.
SSHD_ENTRY = (
    "mkdir -p /run/sshd && export -p > /run/testkit-env.sh && "
    "exec /usr/sbin/sshd -D -e -p \"$TESTKIT_SSH_PORT\" -h {d}/host_key "
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
                 group_pre=None, host_collect=None, host_task=None):
        self.mode = mode
        self.group_prepare = None           # group_prepare(runner) -> error or None, after host_collect
        self.host_task = host_task          # host mode: host_task(runner) -> (status, reason), runs on rank 0's host
        self.counters = None                # link counter settings: snapshot before/after on every host
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
        "TESTKIT_RUN_ID": run_id, "TESTKIT_ITEM": item_id, "TESTKIT_GROUP": g.id,
        "TESTKIT_NODE_RANK": str(node.rank), "TESTKIT_NNODES": str(len(g.hosts)),
        "TESTKIT_HOSTNAME": node.host.name, "TESTKIT_MASTER_ADDR": g.master.mgmt_ip,
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
        extra = {"TESTKIT_SMOKE_SLEEP": str(cfg.get("sleep", 0)), "TESTKIT_SMOKE_EXIT": str(cfg.get("exit_code", 0))}
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
    if item.id == "links":
        return links_job(ctx, item, cfg, nodes)
    if item.section == "nvlink":
        return nvlink_job(ctx, item, cfg, nodes)
    if item.section == "nvshmem":
        return nvshmem_job(ctx, item, cfg, nodes, image)
    if item.section == "nixl":
        return nixl_job(ctx, item, cfg, nodes, image)
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
    from testkit.nodecmd import script
    return script(name).decode()


def dcgm_job(ctx, item, cfg, nodes, image):
    """S5: dcgmi diag + nvidia-smi checks in the official DCGM image; parsed by testkit.parsers.dcgm."""
    from testkit.parsers import dcgm
    tests, run_id = ctx.tests, ctx.run_id
    mode = cfg.get("hostengine", "container")
    env = {"TESTKIT_DCGM_MODE": mode, "TESTKIT_DCGM_LEVEL": str(cfg.get("level", 3)),
           "TESTKIT_DCGM_HEALTH": "1" if cfg.get("health", True) else "0"}
    text = node_script("dcgm_run.sh")
    specs, files = [], []
    for n in nodes:
        tool_dir = node_dir(tests, run_id, item.id, n.host.name) + ".testkit"
        files.append((n.host, tool_dir + "/dcgm_run.sh", text, "755"))
        mounts = [(tool_dir, "/testkit", True)]
        if mode == "host":
            mounts.append(("/var/log/nvidia-dcgm", "/host-dcgm-log", True))
        spec = _spec(tests, run_id, item.id, n, image, ["/testkit/dcgm_run.sh"], env, mounts=mounts)
        spec.extra_args = ["--entrypoint", "sh"]
        specs.append(spec)
    flag = "/run/testkit-dcgm-stopped-" + run_id
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
        lines.append("{} ansible_host={} testkit_name={}".format(n.host.mgmt_ip or n.host.ssh, n.host.ssh, n.host.name))
    lines += ["", "[all:vars]", "ansible_user={}".format(ssh_cfg.get("user", "root")),
              "ansible_port={}".format(ssh_cfg.get("port", 22)), "ansible_ssh_private_key_file={}".format(key_path),
              "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'", ""]
    return "\n".join(lines)


def superbench_job(ctx, item, cfg, nodes, image, worker=None, parser=None):
    """S6: sbcli container on the rank 0 host runs sb deploy/run against the whole group.

    A key made for this run is added to every host's authorized_keys before and removed after.
    """
    from testkit.parsers import superbench
    tests, run_id = ctx.tests, ctx.run_id
    priv, pub, _ = ctx.ssh_keys()
    master = nodes[0]
    tool_dir = node_dir(tests, run_id, item.id, master.host.name) + ".testkit"
    files = [(master.host, tool_dir + "/inventory", inventory(nodes, ctx.cluster.ssh, "/testkit/id_ed25519"), "644"),
             (master.host, tool_dir + "/id_ed25519", priv, "600"),
             (master.host, tool_dir + "/sb_run.sh", node_script("sb_run.sh"), "755")]
    worker = worker or tests.images.get("sb_worker") or ""
    bench = cfg.get("benchmarks") or {}
    env = {"TESTKIT_SB_CONFIG": cfg.get("config", ""), "TESTKIT_SB_WORKER": worker,
           "TESTKIT_SB_RULE": cfg.get("rule", ""), "TESTKIT_SB_INCLUDE": ",".join(bench.get("include") or []),
           "TESTKIT_SB_EXCLUDE": ",".join(bench.get("exclude") or [])}
    if cfg.get("config_dir"):
        env["TESTKIT_SB_CONFIG_DIR"] = cfg["config_dir"]
    spec = _spec(tests, run_id, item.id, master, image, ["/testkit/sb_run.sh"], env,
                 mounts=[(tool_dir, "/testkit", True)])
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
    own = item.id in OWN_RESULTS

    def full_mapping():
        # SuperBench names hosts by their short OS hostname (Ansible facts), which may differ from cluster.yaml.
        out = dict(mapping)
        out.update(_host_names(ctx, item.id, nodes))
        return out

    def rails():
        from testkit.parsers.nccl_log import rails_by_gpu
        from testkit.state import load_nic
        return {n.host.name: rails_by_gpu(load_nic(tests.results_dir, n.host.name)) for n in nodes}
    if parser is None:
        def parser(local_dir):
            return superbench.parse_group(local_dir, full_mapping(), own, cfg, rails=rails())
    else:
        sharp_parser = parser

        def parser(local_dir):
            return sharp_parser(local_dir, mapping=full_mapping(), cfg=cfg, rails=rails())
    job = Job("delegate", [spec], files=files, group_result=True, host_pre=pre, host_post=post,
              host_images=[worker] if worker else [], timeout=cfg.get("timeout"), parse_group=parser)
    job.host_collect = [("hostname.txt", "hostname")]
    pairing = cfg.get("ib_pairs", IB_PAIRS_DEFAULT.get(item.id, "round_robin"))
    if pairing != "round_robin":
        _ib_pairs(ctx, item, cfg, nodes, job, pairing)
    return job


# Single-node SuperBench runs: each host has its own result (one invocation covers all hosts).
OWN_RESULTS = ("superbench.single_node", "superbench.nccl_node")
IB_PAIRS_DEFAULT = {"superbench.rack": "rack_adjacent", "superbench.cross_rack": "cross_rack"}
IB_BENCHMARKS = ["ib-traffic:perftest:multinode", "ib-traffic:perftest:multinode-bidir",
                 "ib-traffic:perftest:multinode-lat"]


def _ib_pairs(ctx, item, cfg, nodes, job, pairing):
    """Fixed ib-traffic pairs: a pairing file on every host (under /mnt, which the worker containers mount)."""
    from testkit import ibpairs
    rounds, notes = ibpairs.plan(nodes, pairing, cfg.get("cross_rack_rounds", "all") == "all")
    if not rounds:
        raise NotReady("{}: no host pairs ({})".format(item.id, "; ".join(notes) or pairing))
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", "{}-{}".format(item.id, nodes[0].group.id))
    path = "{}/{}/{}/ib-pairs.txt".format(cfg.get("pairs_dir", "/mnt/testkit").rstrip("/"), ctx.run_id, tag)
    # Space separated "key=value" config overrides for sb run (no spaces inside keys or paths).
    job.containers[0].env["TESTKIT_SB_OVERRIDES"] = " ".join(
        "superbench.benchmarks.{}.parameters.config={}".format(b, path) for b in cfg.get("ib_benchmarks") or IB_BENCHMARKS)

    def prepare(runner):
        names = {}
        for n in nodes:
            try:
                with open(os.path.join(ctx.local_dir(n.host.name, item.id), "raw", "hostname.txt"),
                          encoding="utf-8") as f:
                    words = f.read().split()
            except OSError:
                words = []
            if not words:
                return "no hostname from {}".format(n.host.name)
            names[n.host.name] = words[0].split(".")[0]
        dup = sorted({v for v in names.values() if list(names.values()).count(v) > 1})
        if dup:
            return "hosts share the hostname {}; SuperBench needs unique hostnames".format(", ".join(dup))
        text = ibpairs.config_text(rounds, names)
        table = "".join("{}\t{}\t{}\n".format(r, a.host.name, b.host.name)
                        for r, pairs in enumerate(rounds) for a, b in pairs)
        runner.save_raw(nodes[0].host.name, "ib-pairs.txt", text)
        runner.save_raw(nodes[0].host.name, "ib-pairs-hosts.tsv", "round\thost_a\thost_b\n" + table +
                        "".join("# {}\n".format(n) for n in notes))
        for n in nodes:
            res = runner.ops.put(n.host, path, text, "644")
            if not res.ok:
                return "cannot write {} on {}: {}".format(path, n.host.name, res.message())
        for note in notes:
            runner.ctx.log("  {} [{}]: {}".format(item.id, nodes[0].group.id, note))
        return None
    job.group_prepare = prepare
    job.host_post = (job.host_post or "true") + "; rm -rf {}".format(shlex.quote(os.path.dirname(path)))


SHARP_CASES = {"sharp.scale_out": ["O-A", "O-B", "O-C", "O-D"]}
SHARP_OPS = ["allreduce", "reducescatter", "allgather"]


def sharp_job(ctx, item, cfg, nodes, image):
    """S7a: SuperBench nccl-bw:sharp:<op>:<case> from config_gb300_sharp.yaml, one case per NCCL setting."""
    from testkit.parsers import sharp
    cfg = dict(cfg)
    cfg.setdefault("config", "config_gb300_sharp.yaml")
    cases = cfg.get("cases") or SHARP_CASES[item.id]
    ops = cfg.get("ops") or SHARP_OPS
    cfg["benchmarks"] = {"include": ["nccl-bw:sharp:{}:{}".format(op, c) for c in cases for op in ops], "exclude": []}
    return superbench_job(ctx, item, cfg, nodes, image, worker=cfg.get("worker_image") or None,
                          parser=sharp.parse_group)


def _mpi_job(ctx, item, nodes, image, launcher, tools=None, launcher_env=None):
    """tools: {file name: text} written to every host and mounted read-only at /testkit."""
    tests, run_id = ctx.tests, ctx.run_id
    keys = ctx.ssh_keys()
    files = _mpi_files(tests, run_id, item.id, nodes, keys)
    port = str(tests.runtime["mpi_ssh_port"])
    containers = []
    for n in nodes:
        base = node_dir(tests, run_id, item.id, n.host.name)
        mounts = [(base + ".ssh", SSH_DIR, True)]
        if tools:
            mounts.append((base + ".testkit", "/testkit", True))
            for name, text in tools.items():
                files.append((n.host, "{}.testkit/{}".format(base, name), text, "755" if name.endswith(".sh") else "644"))
        containers.append(_spec(tests, run_id, item.id, n, image, ["sh", "-c", SSHD_ENTRY],
                                {"TESTKIT_SSH_PORT": port}, mounts=mounts))
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
    from testkit.parsers import imex
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
    env = {"TESTKIT_IMEX_T1": "1" if bench.get("t1_fabric_ipc", True) else "0",
           "TESTKIT_IMEX_T2": "1" if bench.get("t2_nvbandwidth", False) else "0",
           "TESTKIT_IMEX_GPUS": str(nodes[0].host.gpus), "TESTKIT_NNODES": str(len(nodes))}
    if bench.get("nvbandwidth"):
        env["TESTKIT_NVBANDWIDTH"] = bench["nvbandwidth"]
    job = _mpi_job(ctx, item, nodes, image, "sh /testkit/imex_bench.sh",
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


# ---------------- L0 links ----------------

def links_job(ctx, item, cfg, nodes):
    """L0: read NVLink/IB state and error counters on every host over ssh (no container)."""
    from testkit.executor import pmap
    from testkit.nodecmd import NodeError
    from testkit.parsers import links
    from testkit.state import load_nic, read_json, write_json

    def task(runner):
        def one(n):
            try:
                return n, runner.ops.host_script(n.host, "link_counters.py", {"mlxlink": cfg.get("mlxlink", True)},
                                                 timeout=600), None
            except NodeError as e:
                return n, None, str(e)
        bad = []
        for n, snap, err in pmap(one, runner.nodes, ctx.parallel):
            if snap is None:
                runner.save_raw(n.host.name, "link-counters.err", err)
                bad.append("{}: {}".format(n.host.name, err[:200]))
            else:
                write_json(os.path.join(ctx.local_dir(n.host.name, item.id), "raw", "link-counters.json"), snap)
        return ("error", "; ".join(bad)) if bad else ("pass", "")

    def parse(_):
        per_host = {}
        for n in nodes:
            h = n.host.name
            snap = read_json(os.path.join(ctx.local_dir(h, item.id), "raw", "link-counters.json"))
            if snap is None:
                per_host[h] = {"status": "error", "reason": "link counters not read", "own": True,
                               "metrics": {}, "checks": {}}
                continue
            s = links.state_checks(snap, cfg, n.host, load_nic(ctx.tests.results_dir, h))
            s["own"] = True
            per_host[h] = s
        return per_host, {}

    return Job("host", [], host_task=task, parse_group=parse, group_result=True)


# ---------------- S7b NVLink (dcgmi mndiag) ----------------

NVLOOM_SUITES = ["gpu-to-rack", "pairwise", "all-to-one", "multicast", "latency", "fabric-stress"]
PLUGIN_DIR = "/usr/libexec/datacenter-gpu-manager-4/plugins/cuda13"
HOSTENGINE = "/usr/bin/nv-hostengine -n --service-account nvidia-dcgm"
DROPIN_DIR = "/run/systemd/system/nvidia-dcgm.service.d"


def nvlink_steps(item, cfg, hostfile, head):
    """dcgmi mndiag commands of one nvlink item, in order."""
    base = ["dcgmi", "mndiag", "--hostEngineAddress", head, "--hostfile", hostfile]
    steps = []
    if item.id == "nvlink.pairs":
        ap = cfg.get("all_pairs") or {}
        if ap.get("enabled", True):
            t = int(ap.get("time_to_run", 3600))
            params = ["mnnvbandwidth.gpuPairs=all", "mnnvbandwidth.time_to_run={}".format(t)]
            steps.append({"name": "pairs", "kind": "pairs", "timeout": t + 600,
                          "argv": base + ["--run", "mnnvbandwidth", "--parameters", ";".join(params)]})
        bw = cfg.get("bandwidth") or {}
        if bw.get("enabled", True):
            t = int(bw.get("time_to_run", 3600))
            params = ["mnnvbandwidth.time_to_run={}".format(t)]
            if bw.get("target_num_pairs", -1) is not None:
                params.append("mnnvbandwidth.targetNumPairs={}".format(int(bw.get("target_num_pairs", -1))))
            if bw.get("testcases"):
                params.append("mnnvbandwidth.testcase=" + " ".join(bw["testcases"]))
            if bw.get("buffer_size_mib"):
                params.append("mnnvbandwidth.bufferSize={}".format(int(bw["buffer_size_mib"])))
            steps.append({"name": "bandwidth", "kind": "mndiag", "timeout": t + 660,
                          "argv": base + ["--run", "mnnvbandwidth", "--parameters", ";".join(params), "--json"]})
    else:
        ttr = cfg.get("time_to_run") or {}
        rich = set(cfg.get("rich_output") or [])
        for suite in cfg.get("suites") or NVLOOM_SUITES:
            t = int(ttr.get(suite, ttr.get("default", 1800))) if isinstance(ttr, dict) else int(ttr)
            params = ["nvloom.suite={}".format(suite), "nvloom.time_to_run={}".format(t)]
            if suite in rich:
                params.append("nvloom.rich_output=")
            steps.append({"name": "nvloom-" + suite, "kind": "mndiag", "timeout": t + 660,
                          "argv": base + ["--run", "nvloom", "--parameters", ";".join(params), "--json"]})
    return steps


def poll_detached(runner, host, unit, done_path, timeout):
    """Wait for a systemd-run unit that writes done_path when finished. Returns (status, reason, doc)."""
    deadline = time.time() + timeout
    interval = float(runner.rt["poll_interval"])
    gone = 0
    cmd = "cat {d} 2>/dev/null; echo; echo UNIT=$(systemctl is-active {u} 2>/dev/null)".format(
        d=shlex.quote(done_path), u=shlex.quote(unit))
    while True:
        if runner.ctx.cancelled():
            runner.ops.host_run(host, "systemctl stop {} 2>/dev/null; true".format(shlex.quote(unit)), timeout=120)
            return "cancelled", "run cancelled", None
        if time.time() > deadline:
            runner.ops.host_run(host, "systemctl stop {} 2>/dev/null; true".format(shlex.quote(unit)), timeout=120)
            return "timeout", "not finished after {}s".format(int(timeout)), None
        res = runner.ops.host_run(host, cmd, timeout=60)
        text, _, state = (res.out or "").rpartition("UNIT=")
        start = text.find("{")
        if start >= 0:
            try:
                return "pass", "", json.loads(text[start:])
            except ValueError:
                pass
        if res.ok and state.strip() not in ("active", "activating", "reloading"):
            gone += 1
            if gone >= 3:
                return "error", "{} stopped without writing {} (journalctl -u {})".format(unit, done_path, unit), None
        runner.ctx.cancel.wait(interval)


def _host_names(ctx, item_id, nodes):
    """OS hostname / address -> testkit host name, from cluster.yaml and the hostname.txt of every host."""
    out = {}
    for n in nodes:
        name = n.host.name
        for key in (name, n.host.mgmt_ip, n.host.ssh):
            if key:
                out[key] = name
        try:
            with open(os.path.join(ctx.local_dir(name, item_id), "raw", "hostname.txt"), encoding="utf-8") as f:
                for word in f.read().split():
                    out.setdefault(word, name)
                    out.setdefault(word.split(".")[0], name)
        except OSError:
            pass
    return out


def nvlink_job(ctx, item, cfg, nodes):
    """S7b: dcgmi mndiag on the rank 0 host with the hosts' DCGM (multinode packages from the ISO).

    Every host: nv-hostengine listens on its mgmt address while the item runs (systemd drop-in in /run),
    and the run's public key is authorized. Rank 0: a run key and ssh_config.d entry let Open MPI reach the
    other hosts as root. All of it is removed afterwards.
    """
    from testkit.parsers import nvlink
    tests, run_id = ctx.tests, ctx.run_id
    master = nodes[0]
    head = master.host.mgmt_ip
    if not head or any(not n.host.mgmt_ip for n in nodes):
        raise NotReady("nvlink needs the mgmt address of every host (cluster.yaml mgmt)")
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", "{}-{}".format(item.id, master.group.id))
    share = "{}/{}/{}.testkit-{}".format(tests.runtime["node_results_dir"].rstrip("/"), run_id, item.id, tag)
    out_dir = node_dir(tests, run_id, item.id, master.host.name)
    port = int(cfg.get("hostengine_port", 5555))
    hostfile = "".join("{}{}\n".format(n.host.mgmt_ip, "" if port == 5555 else ":{}".format(port)) for n in nodes)
    steps = nvlink_steps(item, cfg, share + "/hosts.txt", head if port == 5555 else "{}:{}".format(head, port))
    if not steps:
        raise NotReady("{}: no step enabled".format(item.id))
    args = {"out": out_dir, "log_dir": cfg.get("dcgm_log_dir", "/var/log/nvidia-dcgm"),
            "stop_on_fail": cfg.get("stop_on_fail", True), "steps": steps}
    priv, pub, _ = ctx.ssh_keys()
    pub = pub.strip()
    key_tag = pub.split()[-1]
    if not re.match(r"^[A-Za-z0-9._-]+$", key_tag):
        raise NotReady("unexpected ssh key comment {!r}".format(key_tag))
    key_path = "/root/.ssh/testkit-{}-nvlink".format(run_id)
    ssh_conf = "/etc/ssh/ssh_config.d/90-testkit-{}.conf".format(run_id)
    hostengine = cfg.get("hostengine_cmd", HOSTENGINE)
    ssh_port = int(ctx.cluster.ssh.get("port", 22))
    files = []
    for n in nodes:
        dropin = ("[Service]\nExecStart=\nExecStart={} -b {}{}\nEnvironment=\"DCGM_MPIRUN_ALLOW_RUN_AS_ROOT=1\"\n"
                  .format(hostengine, n.host.mgmt_ip, "" if port == 5555 else " -p {}".format(port)))
        files += [(n.host, share + "/bind", n.host.mgmt_ip + "\n", "644"), (n.host, share + "/dropin.conf", dropin, "644")]
    files += [(master.host, share + "/hosts.txt", hostfile, "644"),
              (master.host, share + "/nvlink_run.py", node_script("nvlink_run.py"), "755"),
              (master.host, share + "/args.json", json.dumps(args, indent=1), "644"),
              (master.host, share + "/id_ed25519", priv, "600"),
              (master.host, share + "/ssh.conf", "Host {}\n  User root\n  Port {}\n  IdentityFile {}\n"
               "  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n  BatchMode yes\n  LogLevel ERROR\n"
               .format(" ".join(n.host.mgmt_ip for n in nodes), ssh_port, key_path), "644")]
    plugin = cfg.get("plugin_dir", PLUGIN_DIR)
    need = "mnnvbandwidth" if item.id == "nvlink.pairs" else "nvloom_cli"
    s, q = shlex.quote(share), shlex.quote
    pre = (
        "S={s}; IP=$(cat $S/bind); "
        "command -v mpirun >/dev/null 2>&1 || {{ echo 'mpirun not found (openmpi-bin)'; exit 1; }}; "
        "[ -x {b} ] || {{ echo '{b} not found (datacenter-gpu-manager-4-multinode-cuda13)'; exit 1; }}; "
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
        "(grep -qF {k} ~/.ssh/authorized_keys || echo {k} >> ~/.ssh/authorized_keys); "
        "if [ -f $S/ssh.conf ]; then install -m 600 $S/id_ed25519 {kp} && install -m 644 $S/ssh.conf {sc}; fi; "
        "if systemctl is-active --quiet nvidia-dcgm; then echo active; else echo inactive; fi > $S/dcgm-was; "
        "mkdir -p {dd} && install -m 644 $S/dropin.conf {dd}/90-testkit.conf && systemctl daemon-reload && "
        "systemctl restart nvidia-dcgm || {{ echo 'cannot restart nvidia-dcgm with the testkit drop-in'; exit 1; }}; "
        "i=0; until dcgmi discovery --host \"$IP{p}\" -l >/dev/null 2>&1; do i=$((i+1)); "
        "[ $i -ge 60 ] && {{ echo \"nv-hostengine does not answer on $IP{p}\"; exit 1; }}; sleep 1; done"
    ).format(s=s, b=q(plugin + "/" + need), k=q(pub), kp=q(key_path), sc=q(ssh_conf), dd=DROPIN_DIR,
             p="" if port == 5555 else ":{}".format(port))
    post = (
        "S={s}; rm -f {dd}/90-testkit.conf {kp} {sc} $S/id_ed25519; systemctl daemon-reload; "
        "if [ \"$(cat $S/dcgm-was 2>/dev/null)\" = inactive ]; then systemctl stop nvidia-dcgm; "
        "else systemctl restart nvidia-dcgm; fi; sed -i {d} ~/.ssh/authorized_keys; true"
    ).format(s=s, dd=DROPIN_DIR, kp=q(key_path), sc=q(ssh_conf), d=q("/ {}$/d".format(key_tag.replace(".", "\\."))))
    unit = "testkit-" + re.sub(r"[^A-Za-z0-9_.-]", "_", "{}-{}".format(run_id, tag))
    timeout = int(cfg.get("timeout") or sum(st["timeout"] for st in steps) + 300)

    def task(runner):
        host = runner.nodes[0].host
        res = runner.ops.host_run(host, "systemctl reset-failed {u} 2>/dev/null; systemd-run --unit {u} --collect "
                                  "--quiet /usr/bin/python3 {s}/nvlink_run.py {s}/args.json".format(u=q(unit), s=s),
                                  timeout=60)
        if not res.ok:
            return "error", "cannot start {}: {}".format(unit, res.message())
        status, reason, _ = poll_detached(runner, host, unit, out_dir + "/done.json", timeout)
        if status != "cancelled":
            res = runner.ops.fetch(host, out_dir, ctx.local_dir(host.name, item.id))
            if not res.ok and status == "pass":
                return "error", "results not copied: " + res.message()
        return status, reason

    def parse(local_dir):
        return nvlink.parse_group(local_dir, item.id, cfg, _host_names(ctx, item.id, nodes),
                                  [n.host.name for n in nodes], sum(n.host.gpus for n in nodes))

    job = Job("host", [], files=files, host_pre=pre, host_post=post, host_task=task, parse_group=parse,
              group_result=True, timeout=timeout)
    job.host_collect = [("hostname.txt", "hostname; hostname -s; hostname -I")]
    return job


# ---------------- S7b NVSHMEM (optional) ----------------

NVSHMEM_TESTS = {"nvshmem.pair": ["pt-to-pt/shmem_put_bw", "pt-to-pt/shmem_get_bw", "pt-to-pt/shmem_put_latency",
                                  "pt-to-pt/shmem_get_latency", "pt-to-pt/shmem_p_latency"],
                 "nvshmem.rack": ["coll/alltoall_latency"]}


def nvshmem_job(ctx, item, cfg, nodes, image):
    """NVSHMEM perftests in mpi-mode containers (moe-bench image with libnvshmem3-dev-cuda-13)."""
    from testkit.parsers import nvshmem
    ppn = cfg.get("ppn", 1 if item.id == "nvshmem.pair" else nodes[0].host.gpus)
    env = {"TESTKIT_NVSHMEM_TESTS": " ".join(cfg.get("tests") or NVSHMEM_TESTS[item.id]),
           "TESTKIT_NVSHMEM_PPN": str(ppn), "TESTKIT_NNODES": str(len(nodes))}
    if cfg.get("perftest_dir"):
        env["TESTKIT_NVSHMEM_DIR"] = cfg["perftest_dir"]
    if cfg.get("perftest_args"):
        env["TESTKIT_NVSHMEM_ARGS"] = " ".join(str(a) for a in cfg["perftest_args"])
    for key, value in (cfg.get("nvshmem_env") or {}).items():
        env[str(key)] = str(value)
    job = _mpi_job(ctx, item, nodes, image, "sh /testkit/nvshmem_run.sh",
                   tools={"nvshmem_run.sh": node_script("nvshmem_run.sh")}, launcher_env=env)
    job.parse_group = lambda local_dir: nvshmem.parse_group(local_dir)
    return job


# ---------------- NIXL (optional) ----------------
# kind: local = two processes on one host (GPU A -> GPU B), pair = initiator on host A, target on host B,
# storage = one process against a file (no etcd). Sizes follow the testNixl S1-S6 smoke suite.
MIB = 1 << 20
NIXL_CASES = {
    "s1": {"kind": "local", "init": "VRAM", "target": "VRAM", "op": "WRITE", "total": 256 * MIB,
           "start": 4096, "max": 64 * MIB},
    "s2": {"kind": "local", "init": "VRAM", "target": "VRAM", "op": "READ", "check": True, "total": 64 * MIB,
           "start": 4096, "max": 16 * MIB},
    "s3": {"kind": "pair", "init": "DRAM", "target": "DRAM", "op": "WRITE", "total": 1024 * MIB,
           "start": 4096, "max": 64 * MIB},
    "s4": {"kind": "pair", "init": "DRAM", "target": "VRAM", "op": "WRITE", "total": 64 * MIB,
           "start": 4096, "max": MIB},
    "s5": {"kind": "pair", "init": "VRAM", "target": "VRAM", "op": "WRITE", "check": True, "total": 64 * MIB,
           "start": 4096, "max": MIB},
    "s6": {"kind": "pair", "init": "DRAM", "target": "DRAM", "op": "WRITE", "total": 1024 * MIB,
           "start": 64 * 1024, "max": 64 * MIB, "iter": 200},
    "posix": {"kind": "storage", "backend": "POSIX", "init": "DRAM", "op": "WRITE", "total": 1024 * MIB,
              "start": 4096, "max": 16 * MIB},
    "posix_read": {"kind": "storage", "backend": "POSIX", "init": "DRAM", "op": "READ", "check": True,
                   "total": 1024 * MIB, "start": 4096, "max": 16 * MIB},
    "gds": {"kind": "storage", "backend": "GDS", "init": "VRAM", "op": "WRITE", "direct": True,
            "total": 1024 * MIB, "start": 4096, "max": 16 * MIB},
    "gds_read": {"kind": "storage", "backend": "GDS", "init": "VRAM", "op": "READ", "check": True, "direct": True,
                 "total": 1024 * MIB, "start": 4096, "max": 16 * MIB},
}
NIXL_KIND = {"nixl.node": "local", "nixl.pair": "pair", "nixl.storage": "storage"}
NIXL_DEFAULT_CASES = {"nixl.node": ["s1", "s2"], "nixl.pair": ["s3", "s4", "s5", "s6"],
                      "nixl.storage": ["posix", "posix_read"]}
NIXL_PAIRINGS = ("sequential", "rack_adjacent", "rack_half", "cross_rack")


def nixl_pairs(nodes, mode="sequential"):
    """(pairs, notes) run one after another; the first node of a pair is the initiator.

    sequential: nodes in rank order (h1,h2) (h3,h4) ...; an odd host is paired with h1 again.
    Other modes come from testkit.ibpairs (rounds flattened, pairs still run one at a time).
    """
    if mode == "sequential":
        pairs = [(nodes[i], nodes[i + 1]) for i in range(0, len(nodes) - 1, 2)]
        notes = []
        if len(nodes) % 2 and len(nodes) > 1:
            pairs.append((nodes[0], nodes[-1]))
            notes.append("{} hosts: {} paired again with {}".format(len(nodes), nodes[-1].host.name,
                                                                   nodes[0].host.name))
        return pairs, notes
    from testkit import ibpairs
    rounds, notes = ibpairs.plan(nodes, mode)
    return [p for r in rounds for p in r], notes


def nixl_case_args(name, cfg):
    """nixlbench arguments of one case (without --etcd_endpoints, added by nixl_run.sh)."""
    c = NIXL_CASES[name]
    backend = c.get("backend") or cfg.get("backend", "UCX")
    args = ["--backend", backend, "--initiator_seg_type", c["init"], "--op_type", c["op"],
            "--total_buffer_size", str(c["total"]), "--start_block_size", str(c["start"]),
            "--max_block_size", str(c["max"]),
            "--num_iter", str(c.get("iter", cfg.get("num_iter", 100))),
            "--warmup_iter", str(cfg.get("warmup_iter", 10))]
    if c["kind"] == "storage":
        # A directory: nixlbench creates nixlbench_<backend>_test_file_* there, READ reuses the WRITE file
        args += ["--filepath", str(cfg.get("filepath", ""))]
        if c.get("direct"):
            args.append("--storage_enable_direct")
    else:
        args += ["--target_seg_type", c["target"], "--benchmark_group", name]
        if c["kind"] == "pair" and cfg.get("device_list", "all") != "all":
            args += ["--device_list", str(cfg["device_list"])]
    if c.get("check"):
        args.append("--check_consistency")
    args += [str(a) for a in cfg.get("nixlbench_args") or []]
    args += [str(a) for a in (cfg.get("case_args") or {}).get(name) or []]
    return args


def nixl_job(ctx, item, cfg, nodes, image):
    """NIXLBench in mpi-mode containers (nixl-benchmark image); rank 0 runs node_scripts/nixl_run.sh.

    testkit writes the case list (and the host pairs) to /testkit; nixl_run.sh starts etcd in the rank 0
    container and every nixlbench process (over ssh for other hosts), and saves the output to /results/raw.
    """
    from testkit.parsers import nixl
    kind = NIXL_KIND[item.id]
    names = cfg.get("cases") or NIXL_DEFAULT_CASES[item.id]
    gpus = cfg.get("gpus") or [0, 1]
    rows = []
    for name in names:
        if NIXL_CASES.get(name, {}).get("kind") != kind:
            raise NotReady("{}: case {} is not a {} case".format(item.id, name, kind))
        c = NIXL_CASES[name]
        # CUDA device of the initiator / target process ("-" = not set)
        dev_i = str(gpus[0]) if c["init"] == "VRAM" else "-"
        dev_t = "-"
        if kind == "local":
            dev_t = str(gpus[1]) if c["target"] == "VRAM" else "-"
        elif kind == "pair":
            dev_t = str(gpus[0]) if c["target"] == "VRAM" else "-"
        rows.append("\t".join([name, kind, dev_i, dev_t, " ".join(nixl_case_args(name, cfg))]))
    tools = {"nixl_run.sh": node_script("nixl_run.sh"), "nixl_cases.tsv": "\n".join(rows) + "\n"}
    notes = []
    if kind == "pair":
        pairs, notes = nixl_pairs(nodes, cfg.get("pairs", "sequential"))
        if not pairs:
            raise NotReady("{}: no host pairs ({})".format(item.id, "; ".join(notes) or "needs 2 hosts"))
        tools["nixl_pairs.tsv"] = "".join("{}\t{}\t{}\t{}\n".format(a.host.mgmt_ip, a.host.name, b.host.mgmt_ip,
                                                                    b.host.name) for a, b in pairs)
    env = {"TESTKIT_NIXL_ETCD_PORT": str(cfg.get("etcd_port", 12379)),
           "TESTKIT_NIXL_TIMEOUT": str(cfg.get("case_timeout", 900))}
    job = _mpi_job(ctx, item, nodes, image, "bash /testkit/nixl_run.sh", tools=tools, launcher_env=env)
    if kind == "storage":
        path = cfg.get("filepath")
        if not path:
            raise NotReady("{}: filepath is not set".format(item.id))
        # Same path on the host and in the container; the test files are removed before and after the run.
        for spec in job.containers:
            spec.mounts.append((path, path, False))
        clear = "rm -f {}/nixlbench_*_test_file_*".format(shlex.quote(path.rstrip("/")))
        job.host_pre = "mkdir -p {} && {}".format(shlex.quote(path), clear)
        job.host_post = clear + "; true"
    if kind == "pair":
        expected = ["{}@{}+{}".format(name, a.host.name, b.host.name) for name in names for a, b in pairs]
    else:
        expected = list(names)
    job.parse_group = lambda local_dir: nixl.parse_group(local_dir, cfg, nodes[0].host.name, expected, notes,
                                                         storage=kind == "storage")
    job.timeout = cfg.get("timeout")
    return job
