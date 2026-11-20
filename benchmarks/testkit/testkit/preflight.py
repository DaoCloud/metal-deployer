"""testkit preflight: check that the hosts are ready for the selected items, without running them.

One ssh session per host runs a shell probe (read only). It checks SSH, the Docker daemon, the
NVIDIA container runtime, GPUs and driver, the images the items use, leftover test containers, the
host tools some items call, and TCP reachability of the ssh port from a group's rank 0 host to the
other hosts (SuperBench and nvlink log in from there). With --pull, missing images are fetched as
"testkit run" would (tests.yaml image_pull).
"""

import json
import re
import shlex
from collections import Counter

from testkit import marks
from testkit.docker import IMAGE_INSPECT, parse_image_info
from testkit.executor import counters_cfg, pmap

q = shlex.quote
CHECKS = ("ssh", "docker", "runtime", "gpu", "images", "containers", "tools", "peers")
OK, WARN, FAIL, NA = "ok", "warn", "FAIL", "-"
CONTAINER_PREFIX = "acc-"
PEER_TIMEOUT = 5


class Need:
    """What one host must have for the selected items. Values list the items that need it."""

    def __init__(self, host):
        self.host = host
        self.images = {}        # image ref -> [item ids]
        self.unset = []         # item ids whose image is not set in tests.yaml
        self.tools = {}         # label -> (shell test, [item ids])
        self.peers = {}         # (address, port) -> [item ids], probed from this host
        self.container = False  # some item starts containers here

    def add(self, table, key, item_id):
        ids = table.setdefault(key, [])
        if item_id not in ids:
            ids.append(item_id)

    def tool(self, spec, item_id):
        label, test = spec
        self.tools.setdefault(label, (test, []))
        if item_id not in self.tools[label][1]:
            self.tools[label][1].append(item_id)


def _cmd(name):
    return name, "command -v {} >/dev/null 2>&1".format(q(name))


def _exe(path):
    return path, "test -x {}".format(q(path))


def _unit(name):
    return name + ".service", "systemctl cat {} >/dev/null 2>&1".format(q(name))


def _mode(item, cfg):
    return cfg.get("mode", item.mode) if item.id == "smoke" else item.mode


def requirements(cluster, tests, hosts, plans):
    """{host name: Need} for the selected hosts and item plans (testkit.plan.build_plan)."""
    from testkit.runners import PLUGIN_DIR
    needs = {h.name: Need(h) for h in hosts}
    port = int(cluster.ssh.get("port", 22))
    for plan in plans:
        item, cfg = plan.item, plan.cfg
        mode = _mode(item, cfg)
        counters = counters_cfg(tests, item) is not None
        for g in plan.groups:
            members = [needs[h.name] for h in g.hosts if h.name in needs]
            master = needs.get(g.hosts[0].name) if g.hosts else None
            on = members if mode in ("per-node", "mpi") else ([master] if master and mode == "delegate" else [])
            for n in on:
                n.container = True
                if plan.image:
                    n.add(n.images, plan.image, item.id)
                elif item.id not in n.unset:
                    n.unset.append(item.id)
            if mode == "delegate" and item.section in ("superbench", "sharp"):
                worker = (cfg.get("worker_image") if item.section == "sharp" else None) or tests.images.get("sb_worker")
                for n in members:
                    if worker:
                        n.container = True
                        n.add(n.images, worker, item.id)
                if master:
                    for h in g.hosts:
                        master.add(master.peers, (h.ssh, port), item.id)
            if counters or item.id == "links":
                for n in members:
                    n.tool(_cmd("python3"), item.id)
            if item.id == "links" and cfg.get("mlxlink", True):
                for n in members:
                    n.tool(_cmd("mlxlink"), item.id)
            if item.id == "dcgm" and cfg.get("hostengine", "container") == "host":
                for n in members:
                    n.tool(_unit("nvidia-dcgm"), item.id)
            if item.section == "nvlink":
                need = "mnnvbandwidth" if item.id == "nvlink.pairs" else "nvloom_cli"
                for n in members:
                    for spec in (_cmd("mpirun"), _cmd("dcgmi"), _unit("nvidia-dcgm"),
                                 _exe(cfg.get("plugin_dir", PLUGIN_DIR) + "/" + need)):
                        n.tool(spec, item.id)
                if master:
                    master.tool(_cmd("python3"), item.id)
                    master.tool(_cmd("systemd-run"), item.id)
                    for h in g.hosts:
                        if h.mgmt_ip:
                            master.add(master.peers, (h.mgmt_ip, port), item.id)
            if item.id == "imex":
                scripts = (cfg.get("scripts_dir") or "/opt/resource/scripts").rstrip("/")
                specs = [_cmd("nvidia-imex-ctl"), _exe(scripts + "/imex-check.sh")]
                if cfg.get("configure", True):
                    specs.append(_exe(scripts + "/imex-configure.sh"))
                for n in members:
                    for spec in specs:
                        n.tool(spec, item.id)
    return needs


def gpu_runtime_needed(tests):
    """True if the test containers ask Docker for GPUs (runtime.container_args)."""
    return any("--gpus" in str(a) or "nvidia" in str(a) for a in tests.runtime.get("container_args") or [])


PROBE_HEAD = r"""p() { printf '%s\t%s\n' "$1" "$2"; }
v=$(docker version --format '{{.Server.Version}}' 2>&1); p docker "$?|$(printf '%s' "$v" | head -n 1)"
r=$(docker info --format '{{json .Runtimes}}' 2>/dev/null)
case "$r" in
  *'"nvidia"'*) p runtime "docker runtime nvidia" ;;
  *) if command -v nvidia-container-runtime-hook >/dev/null 2>&1; then p runtime nvidia-container-runtime-hook
     elif command -v nvidia-container-cli >/dev/null 2>&1; then p runtime nvidia-container-cli
     elif ls /etc/cdi/nvidia*.yaml /var/run/cdi/nvidia*.yaml >/dev/null 2>&1; then p runtime "CDI nvidia spec"
     else p runtime ""; fi ;;
esac
if command -v nvidia-smi >/dev/null 2>&1; then
  o=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1); p gpu "$?|$(printf '%s' "$o" | tr '\n' ',')"
else p gpu "127|nvidia-smi not found"; fi
p containers "$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^PREFIX' | tr '\n' ' ')"
"""


def probe_script(need):
    lines = [PROBE_HEAD.replace("PREFIX", CONTAINER_PREFIX)]
    for image in sorted(need.images):
        lines.append("o=$({}{} 2>/dev/null) && p {} \"$o\" || p {} ''".format(
            IMAGE_INSPECT, q(image), q("image:" + image), q("image:" + image)))
    for label, (test, _) in sorted(need.tools.items()):
        lines.append("if {}; then p {} ok; else p {} missing; fi".format(test, q("tool:" + label), q("tool:" + label)))
    for addr, port in sorted(need.peers):
        key = q("peer:{}:{}".format(addr, port))
        lines.append("( if timeout {t} bash -c {c} 2>/dev/null; then p {k} ok; else p {k} fail; fi ) &".format(
            t=PEER_TIMEOUT, c=q(": </dev/tcp/{}/{}".format(addr, port)), k=key))
    lines += ["wait", "exit 0"]
    return "\n".join(lines) + "\n"


def parse_probe(text):
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("\t")
        if sep:
            out[key] = value
    return out


def _probe(ops, need):
    res = ops.host_run(need.host, "bash -c " + q(probe_script(need)), timeout=120)
    if not res.ok:
        return None, res.message() or "exit {}".format(res.rc)
    return parse_probe(res.out), ""


def _fetch(ops, host, image, policy):
    """Get a missing image as testkit run does; returns an error message or ""."""
    if policy == "registry":
        res = ops.pull(host, image)
        return "" if res.ok else "docker pull failed: " + res.message()
    if policy == "push":
        res = ops.push(host, image)
        return "" if res.ok else "push failed: " + res.message()
    return "image_pull: skip"


def check_host(ops, need, tests, pull=False, gpu_runtime=True):
    """{check: (status, detail)} of one host; digests are kept in the "_digests" key."""
    r = {c: (NA, "") for c in CHECKS}
    r["_digests"] = {}
    r["_driver"] = ""
    probe, err = _probe(ops, need)
    if probe is None:
        r["ssh"] = (FAIL, err)
        return r
    r["ssh"] = (OK, "")
    rc, _, version = probe.get("docker", "1|no output").partition("|")
    docker_ok = rc == "0"
    r["docker"] = (OK, version) if docker_ok else (FAIL, version or "docker not available")

    if need.container and gpu_runtime:
        rt = probe.get("runtime", "")
        r["runtime"] = (OK, rt) if rt else (FAIL, "no NVIDIA container runtime (nvidia-container-toolkit)")

    rc, _, out = probe.get("gpu", "1|no output").partition("|")
    drivers = [d.strip() for d in out.split(",") if d.strip()]
    if rc != "0" or not drivers:
        r["gpu"] = (FAIL, out.strip(",") or "nvidia-smi failed")
    elif len(drivers) != need.host.gpus:
        r["gpu"] = (FAIL, "{} GPUs, expected {}".format(len(drivers), need.host.gpus))
    else:
        r["_driver"] = drivers[0]
        r["gpu"] = (OK, "{} GPUs, driver {}".format(len(drivers), drivers[0]))

    if docker_ok:
        busy = probe.get("containers", "").split()
        r["containers"] = (FAIL, "running: " + ", ".join(busy[:3])) if busy else (OK, "")
        if need.images or need.unset:
            bad = ["{}: image not set in tests.yaml".format(i) for i in need.unset]
            policy = tests.data.get("image_pull", "registry")
            for image in sorted(need.images):
                info = probe.get("image:" + image, "")
                if not info and pull:
                    msg = _fetch(ops, need.host, image, policy)
                    if not msg:
                        present, digest = ops.image_info(need.host, image)
                        if present:
                            r["_digests"][image] = digest
                            continue
                        msg = "not present after fetch"
                    bad.append("{} ({})".format(image, msg))
                elif not info:
                    bad.append(image + " missing")
                else:
                    r["_digests"][image] = parse_image_info(info)
            r["images"] = (FAIL, "; ".join(bad)) if bad else (OK, "{} image(s)".format(len(need.images)))

    if need.tools:
        missing = ["{} ({})".format(label, ",".join(ids)) for label, (_, ids) in sorted(need.tools.items())
                   if probe.get("tool:" + label) != "ok"]
        r["tools"] = (FAIL, "missing: " + ", ".join(missing)) if missing else (OK, "{} tool(s)".format(len(need.tools)))

    if need.peers:
        down = ["{}:{}".format(a, p) for (a, p) in sorted(need.peers) if probe.get("peer:{}:{}".format(a, p)) != "ok"]
        r["peers"] = ((FAIL, "ssh port not reachable from here: " + ", ".join(down)) if down
                      else (OK, "{} host(s)".format(len(need.peers))))
    return r


def _majority_warn(results, values, check, what):
    """Warn the hosts whose value differs from the most common one."""
    if len(set(values.values())) < 2:
        return
    common = Counter(values.values()).most_common(1)[0][0]
    for name, value in values.items():
        status, detail = results[name][check]
        if value != common and status == OK:
            results[name][check] = (WARN, "{} {} differs from most hosts ({}); {}".format(what, value, common, detail))


def run_checks(ops, needs, tests, pull=False, parallel=24):
    gpu_runtime = gpu_runtime_needed(tests)
    results = dict(pmap(lambda n: (n.host.name, check_host(ops, n, tests, pull, gpu_runtime)),
                        list(needs.values()), parallel))
    drivers = {h: r["_driver"] for h, r in results.items() if r["_driver"]}
    _majority_warn(results, drivers, "gpu", "driver")
    images = {i for r in results.values() for i in r["_digests"]}
    for image in sorted(images):
        digests = {h: r["_digests"][image] for h, r in results.items() if image in r["_digests"]}
        _majority_warn(results, digests, "images", image + " digest")
    return results


def _short(text, width=60):
    return text if len(text) <= width else text[:width - 3] + "..."


def format_results(results, icons=None):
    names = sorted(results, key=lambda n: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", n)])
    width = max([len(n) for n in names] + [4])
    cols = [max(len(c), 4) for c in CHECKS]
    cell = lambda status: marks.icon(status, icons) or status
    lines = ["  ".join([marks.pad("host", width)] + [marks.pad(c, w) for c, w in zip(CHECKS, cols)]).rstrip()]
    problems = []
    for name in names:
        r = results[name]
        lines.append("  ".join([marks.pad(name, width)]
                               + [marks.pad(cell(r[c][0]), w) for c, w in zip(CHECKS, cols)]).rstrip())
        for c in CHECKS:
            if r[c][0] in (FAIL, WARN):
                problems.append("{}  {} {}: {}".format(marks.pad(name, width), marks.labeled(r[c][0], icons), c,
                                                       r[c][1]))
    if problems:
        lines += ["", "problems:"] + ["  " + p for p in problems]
    return "\n".join(lines) + "\n"


def summary(results):
    failed = sorted(h for h, r in results.items() if any(r[c][0] == FAIL for c in CHECKS))
    warned = sum(1 for r in results.values() for c in CHECKS if r[c][0] == WARN)
    return failed, warned


def to_json(results):
    failed, _ = summary(results)
    hosts = {h: {c: {"status": r[c][0], "detail": r[c][1]} for c in CHECKS} for h, r in results.items()}
    return {"ready": not failed, "hosts": hosts}


def run(cluster, tests, hosts, plans, ops, pull=False, as_json=False, out=print):
    needs = requirements(cluster, tests, hosts, plans)
    results = run_checks(ops, needs, tests, pull=pull, parallel=int(cluster.ssh.get("parallel", 24)))
    failed, warned = summary(results)
    if as_json:
        out(json.dumps(to_json(results), indent=1, sort_keys=True))
    else:
        items = ", ".join(p.item.id for p in plans) or "-"
        out("items: " + items)
        out(format_results(results).rstrip("\n"))
        out("result: {} hosts, {} ready, {} not ready, {} warning(s)".format(
            len(results), len(results) - len(failed), len(failed), warned))
    return 1 if failed else 0
