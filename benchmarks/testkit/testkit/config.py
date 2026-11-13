"""Load and check network.yaml (hosts and addresses) and tests.yaml (tests and host settings)."""

import ipaddress
import os
import re

from testkit import platforms, scaffold
from testkit.util import ConfigError, Problems, deep_merge, env_str, load_yaml, natural_key, unknown_keys

RUNTIME_DEFAULTS = {
    "results_dir": "./runs",
    "node_results_dir": "/var/lib/testkit/runs",
    "container_args": ["--gpus", "all", "--network", "host", "--ipc", "host", "--privileged",
                       "--ulimit", "memlock=-1", "--ulimit", "stack=67108864", "--shm-size", "64g"],
    "master_port_base": 29500,
    "mpi_ssh_port": 2222,
    "rendezvous_timeout": 600,
    "run_timeout": 7200,
    "poll_interval": 3,
    "stop_on_failure": True,
    "group_order": "serial",
}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# tests.yaml "hosts": optional test settings of single hosts, keyed by the name in network.yaml.
HOST_FIELDS = ("tags", "env", "skip")
# A host without a rack in network.yaml is in this rack.
NO_RACK = "all"
NIXL_FREE = {"nixl.{}.{}".format(sub, key) for sub in ("common", "node", "pair", "storage")
             for key in ("min_bw_gbps", "max_lat_us", "case_args")}
TESTS_FREE = {"images", "common_env", "hosts", "nic.expect", "sharp.common.env", "sharp.scale_out.cases",
              "sharp.scale_out.sharp_env", "moe.common.env", "moe.common.per_rank",
              "nvlink.fabric.time_to_run", "nvshmem.common.nvshmem_env", "nvshmem.pair.nvshmem_env",
              "nvshmem.rack.nvshmem_env"} | NIXL_FREE | {"{}.{}.baseline".format(s, sub) for s, subs in (
                  ("superbench", ("common", "single_node", "disk", "pair", "rack", "cross_rack", "nccl_node", "nvl_domain",
                                  "all", "soak")),
                  ("nvlink", ("common", "pairs", "fabric")), ("sharp", ("common", "scale_out")))
                  for sub in subs}


class Host:

    def __init__(self, name, rack, tray, ssh, mgmt=None, storage=None, ib=None, serial="", tags=None,
                 env=None, skip=None, gpus=4):
        self.name = name
        self.rack = rack
        self.tray = tray
        self.ssh = ssh
        self.mgmt = mgmt            # ipaddress.IPv4Interface or None
        self.storage = storage
        self.ib = ib or []
        self.serial = serial
        self.tags = tags or []
        self.env = env or {}
        self.skip = skip or []
        self.gpus = gpus
        self.pci = {}               # from network.yaml: {"mgmt": pci, "storage": pci or None, "ib": [pci per rail]}

    @property
    def mgmt_ip(self):
        return str(self.mgmt.ip) if self.mgmt else ""

    @property
    def golden(self):
        return "golden" in self.tags

    def sort_key(self):
        return (natural_key(self.rack), self.tray if self.tray is not None else float("inf"), natural_key(self.name))

    def __repr__(self):
        return "Host({})".format(self.name)


class Cluster:

    def __init__(self, path, ssh, network, hosts, platform=platforms.DEFAULT):
        self.path = path
        self.platform = platform
        self.ssh = ssh
        self.network = network         # {"expect": ...}: NIC expectations of "testkit nic discover"
        self.network_file = None       # path of the network file the hosts come from
        self.hosts = sorted(hosts, key=Host.sort_key)
        self.by_name = {h.name: h for h in self.hosts}

    @property
    def racks(self):
        return sorted({h.rack for h in self.hosts}, key=natural_key)

    def rack_hosts(self, rack):
        return [h for h in self.hosts if h.rack == rack]

    @property
    def tags(self):
        return sorted({t for h in self.hosts for t in h.tags})


def _host(nh, extra, rails, gpus, problems):
    """Host from a network.yaml entry (addresses, rack, tray) plus its settings in tests.yaml hosts."""
    where = nh.name
    ok = True
    if not NAME_RE.match(nh.name):
        problems.error("{}: host name may only use letters, digits, '.', '_' and '-'".format(where))
        ok = False
    ports = {p.label: p for p in nh.ports}
    if "mgmt" not in ports:
        problems.error("{}: mgmt is required in the network file (also used as MASTER_ADDR)".format(where))
        ok = False
    ib = nh.rails()
    if ib and len(ib) != rails:
        problems.error("{}: {} compute devices, expected {} (one per rail)".format(where, len(ib), rails))
        ok = False
    if not isinstance(extra, dict):
        problems.error("tests.yaml hosts.{}: must be a mapping".format(where))
        return None
    for key in extra:
        if key not in HOST_FIELDS:
            problems.warn("tests.yaml: unknown key hosts.{}.{}".format(where, key))
    tags = extra.get("tags") or []
    skip = extra.get("skip") or []
    env = extra.get("env") or {}
    for field, value, kind in (("tags", tags, list), ("skip", skip, list), ("env", env, dict)):
        if not isinstance(value, kind):
            problems.error("tests.yaml hosts.{}: {} must be a {}".format(
                where, field, "list" if kind is list else "mapping"))
            ok = False
    if not ok:
        return None
    iface = lambda label: ipaddress.ip_interface(ports[label].ip) if label in ports else None
    host = Host(name=nh.name, rack=nh.rack or NO_RACK, tray=nh.tray, ssh=nh.ssh, mgmt=iface("mgmt"),
                storage=iface("storage"), ib=[ipaddress.ip_interface(p.ip) for p in ib],
                tags=[str(t) for t in tags], env={str(k): env_str(v) for k, v in env.items()},
                skip=[str(x) for x in skip], gpus=gpus)
    host.pci = {"mgmt": ports["mgmt"].pci, "storage": ports["storage"].pci if "storage" in ports else None,
                "ib": [p.pci for p in ib]}
    return host


def load_cluster(path, problems, tests_data=None, platform=platforms.DEFAULT):
    """Hosts from the network file plus the host settings of tests.yaml (golden, hosts, nic.expect).
    Bad hosts are reported and left out; ConfigError if the file is unreadable."""
    from testkit.netcheck import load_network
    from testkit.ssh import settings
    data = tests_data or {}
    where = os.path.basename(path)
    expect = (data.get("nic") or {}).get("expect") or {}
    if not isinstance(expect, dict):
        problems.error("tests.yaml: nic.expect must be a mapping")
        expect = {}
    network = platforms.resolve(platform, {"expect": expect})
    extra = data.get("hosts") or {}
    if not isinstance(extra, dict):
        problems.error("tests.yaml: hosts must be a mapping of host name -> {tags, env, skip}")
        extra = {}
    net, net_problems = load_network(path)
    for msg in net_problems.warnings:
        problems.warn("{}: {}".format(where, msg))
    for msg in net_problems.errors:
        problems.error("{}: {}".format(where, msg))
    names = {h.name for h in net.hosts}
    for name in extra:
        if name not in names:
            problems.error("tests.yaml: hosts.{} is not a host of {}".format(name, where))
    golden = data.get("golden")
    if golden is not None and golden not in names:
        problems.error("tests.yaml: golden {} is not a host of {}".format(golden, where))
    rails = platforms.rails(network)
    gpus = (network.get("expect") or {}).get("gpus") or 4
    hosts = []
    for nh in net.hosts:
        if nh.valid:
            host = _host(nh, extra.get(nh.name) or {}, rails, gpus, problems)
            if host:
                if host.name == golden and "golden" not in host.tags:
                    host.tags.append("golden")
                hosts.append(host)
    cluster = Cluster(path, settings(), network, hosts, platform)
    check_cluster(cluster, problems)
    return cluster


def check_cluster(cluster, problems):
    golden = [h.name for h in cluster.hosts if h.golden]
    if len(golden) > 1:
        problems.error("more than one golden host: {}".format(", ".join(golden)))
    elif not golden and cluster.hosts:
        problems.warn("no golden host (tests.yaml golden); S3 cannot compare NIC mappings")
    slots = {}
    for h in cluster.hosts:
        if h.tray is not None:
            slots.setdefault((h.rack, h.tray), []).append(h.name)
    trays = {h.rack for h in cluster.hosts if h.tray is not None}
    for rack in sorted({h.rack for h in cluster.hosts}, key=natural_key):
        missing = [h.name for h in cluster.hosts if h.rack == rack and h.tray is None]
        if rack in trays and missing:
            problems.warn("rack {}: {} have no tray and are ordered last".format(rack, ", ".join(missing)))


class Tests:

    def __init__(self, path, data, platform=platforms.DEFAULT):
        self.path = path
        self.data = data
        self.platform = platform
        self.base_dir = os.path.dirname(os.path.abspath(path))
        self.runtime = deep_merge(RUNTIME_DEFAULTS, data.get("runtime") or {})
        self.images = data.get("images") or {}
        self.common_env = data.get("common_env") or {}

    @property
    def results_dir(self):
        path = os.path.expanduser(str(self.runtime["results_dir"]))
        return path if os.path.isabs(path) else os.path.normpath(os.path.join(self.base_dir, path))

    def image_for(self, item, cfg):
        """Resolve the image key of an item to a reference; empty string if not set."""
        key = (cfg or {}).get("image") or item.image
        ref = self.images.get(key) or ""
        if not ref and key == "sharp":
            ref = self.images.get("sb_worker") or ""
        return ref


def load_tests_file(path, problems):
    """Load tests.yaml; its platform applies to every host."""
    data = load_yaml(path)
    own = platforms.check(data.get("platform"), os.path.basename(path), problems)
    extra = []
    unknown_keys(data, scaffold.tests_schema(own or platforms.DEFAULT), "", TESTS_FREE, extra)
    for key in extra:
        problems.warn("{}: unknown key {}".format(os.path.basename(path), key))
    return Tests(path, data, own or data.get("platform"))


def load_both(network_path, tests_path, need_tests=True):
    """Load tests.yaml and the network file; returns (cluster, tests, problems).
    A missing tests.yaml is fine if not needed: hosts then get the default platform."""
    problems = Problems()
    tests = None
    if need_tests or os.path.isfile(tests_path):
        try:
            tests = load_tests_file(tests_path, problems)
        except ConfigError as e:
            if need_tests:
                raise
            problems.warn(str(e))
    platform = (tests.platform if tests else None) or platforms.DEFAULT
    if platform not in platforms.PLATFORMS:
        platform = platforms.DEFAULT
    cluster = load_cluster(network_path, problems, tests.data if tests else None, platform)
    return cluster, tests, problems
