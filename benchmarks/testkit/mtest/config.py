"""Load and check cluster.yaml and tests.yaml."""

import ipaddress
import os
import re

from mtest import scaffold
from mtest.util import ConfigError, Problems, deep_merge, dig, env_str, load_yaml, natural_key, unknown_keys

SSH_DEFAULTS = {"user": "root", "key": None, "port": 22, "parallel": 24, "connect_timeout": 10, "options": []}

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
PCI_RE = re.compile(r"^[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# Per-host values from the IP plan; each overrides the matching network-wide setting.
PLAN_FIELDS = ("mgmt_gateway", "storage_gateway", "storage_routes", "lom_gateway", "mgmt_pci", "storage_pci",
               "mgmt_mac", "storage_mac", "ib_pci", "ib_guid", "ib_mtu")
CLUSTER_FREE = {"network.expect", "network.verify", "defaults.env"}
TESTS_FREE = {"images", "common_env", "sharp.common.env", "sharp.scale_up.cases", "sharp.scale_out.cases",
              "sharp.scale_out.sharp_env", "moe.common.env", "moe.common.per_rank"}


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
        self.plan = {}              # PLAN_FIELDS present for this host

    @property
    def mgmt_ip(self):
        return str(self.mgmt.ip) if self.mgmt else ""

    @property
    def golden(self):
        return "golden" in self.tags

    def sort_key(self):
        return (natural_key(self.rack), self.tray, natural_key(self.name))

    def __repr__(self):
        return "Host({})".format(self.name)


class Cluster:

    def __init__(self, path, ssh, network, hosts):
        self.path = path
        self.ssh = ssh
        self.network = network
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


def _iface(value, field, where, problems):
    if value in (None, ""):
        return None
    try:
        iface = ipaddress.ip_interface(str(value))
    except ValueError:
        problems.error("{}: {} '{}' is not a valid address".format(where, field, value))
        return None
    if "/" not in str(value):
        problems.error("{}: {} '{}' needs a prefix length, for example /24".format(where, field, value))
    return iface


def _parse_host(raw, defaults, index, problems, ib_count):
    where = "hosts[{}]".format(index)
    if not isinstance(raw, dict):
        problems.error("{}: must be a mapping".format(where))
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.error("{}: name is required and may only use letters, digits, '.', '_' and '-'".format(where))
        return None
    where = name
    merged = deep_merge(defaults, raw)
    ok = True
    rack = merged.get("rack")
    if rack in (None, ""):
        problems.error("{}: rack is required".format(where))
        ok = False
    tray = merged.get("tray")
    if not isinstance(tray, int) or isinstance(tray, bool) or tray < 1:
        problems.error("{}: tray must be a positive integer".format(where))
        ok = False
    ssh = merged.get("ssh")
    if not ssh:
        problems.error("{}: ssh (LOM address) is required".format(where))
        ok = False
    mgmt = _iface(merged.get("mgmt"), "mgmt", where, problems)
    if merged.get("mgmt") in (None, ""):
        problems.error("{}: mgmt is required (BF3 port 0 address, also used as MASTER_ADDR)".format(where))
        ok = False
    storage = _iface(merged.get("storage"), "storage", where, problems)
    ib_raw = merged.get("ib") or []
    if not isinstance(ib_raw, list):
        problems.error("{}: ib must be a list, one address per rail".format(where))
        ib_raw = []
    if ib_raw and len(ib_raw) != ib_count:
        problems.error("{}: ib has {} addresses, expected {} (one per rail)".format(where, len(ib_raw), ib_count))
    ib = [_iface(v, "ib[{}]".format(i), where, problems) for i, v in enumerate(ib_raw)]
    tags = merged.get("tags") or []
    skip = merged.get("skip") or []
    env = merged.get("env") or {}
    for field, value, kind in (("tags", tags, list), ("skip", skip, list), ("env", env, dict)):
        if not isinstance(value, kind):
            problems.error("{}: {} must be a {}".format(where, field, "list" if kind is list else "mapping"))
            ok = False
    gpus = merged.get("gpus", 4)
    if not isinstance(gpus, int) or gpus < 1:
        problems.error("{}: gpus must be a positive integer".format(where))
        ok = False
    plan = _parse_plan(merged, where, problems, ib_count)
    if not ok or plan is None:
        return None
    host = Host(name=name, rack=str(rack), tray=tray, ssh=str(ssh), mgmt=mgmt, storage=storage, ib=ib,
                serial=env_str(merged.get("serial")), tags=[str(t) for t in tags],
                env={str(k): env_str(v) for k, v in env.items()}, skip=[str(s) for s in skip], gpus=gpus)
    host.plan = plan
    return host


def norm_pci(value):
    """'8:01:00.0' or '00000008:01:00.0' -> '0008:01:00.0'."""
    dom, _, rest = str(value).strip().lower().partition(":")
    try:
        return "{:04x}:{}".format(int(dom, 16), rest) if rest.count(":") == 1 else str(value).strip().lower()
    except ValueError:
        return str(value).strip().lower()


def _parse_plan(merged, where, problems, ib_count):
    plan = {}
    ok = True
    for key in ("mgmt_pci", "storage_pci", "ib_pci", "ib_guid", "mgmt_mac", "storage_mac"):
        vals = merged.get(key)
        vals = vals if isinstance(vals, list) else [vals]
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            problems.error('{}: {} must be quoted in YAML, e.g. "0009:01:00.0"'.format(where, key))
            return None
    for key in ("mgmt_gateway", "storage_gateway", "lom_gateway"):
        if merged.get(key) not in (None, ""):
            try:
                plan[key] = str(ipaddress.ip_address(str(merged[key])))
            except ValueError:
                problems.error("{}: {} '{}' is not a valid address".format(where, key, merged[key]))
                ok = False
    routes = merged.get("storage_routes")
    if routes not in (None, "", []):
        routes = routes if isinstance(routes, list) else re.split(r"[,;\s]+", str(routes).strip())
        plan["storage_routes"] = []
        for r in routes:
            try:
                plan["storage_routes"].append(str(ipaddress.ip_network(str(r))))
            except ValueError:
                problems.error("{}: storage_routes '{}' is not a valid subnet".format(where, r))
                ok = False
    for key in ("mgmt_pci", "storage_pci"):
        if merged.get(key) not in (None, ""):
            plan[key] = norm_pci(merged[key])
            if not PCI_RE.match(plan[key]):
                problems.error("{}: {} '{}' is not a PCI address like 0009:01:00.0".format(where, key, merged[key]))
                ok = False
    for key in ("mgmt_mac", "storage_mac"):
        if merged.get(key) not in (None, ""):
            plan[key] = str(merged[key]).strip().lower().replace("-", ":")
            if not MAC_RE.match(plan[key]):
                problems.error("{}: {} '{}' is not a MAC address".format(where, key, merged[key]))
                ok = False
    for key in ("ib_pci", "ib_guid"):
        vals = merged.get(key)
        if vals in (None, "", []):
            continue
        if not isinstance(vals, list) or len(vals) != ib_count:
            problems.error("{}: {} must be a list of {} values, one per rail".format(where, key, ib_count))
            ok = False
            continue
        if key == "ib_pci":
            vals = [norm_pci(v) if v not in (None, "") else "" for v in vals]
            bad = [v for v in vals if v and not PCI_RE.match(v)]
            if bad:
                problems.error("{}: ib_pci {} is not a PCI address".format(where, bad))
                ok = False
        plan[key] = [env_str(v).lower() for v in vals]
    if merged.get("ib_mtu") not in (None, ""):
        mtu = merged["ib_mtu"]
        if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 65520:
            problems.error("{}: ib_mtu must be an integer between 576 and 65520".format(where))
            ok = False
        else:
            plan["ib_mtu"] = mtu
    return plan if ok else None


def load_cluster(path, problems):
    """Load cluster.yaml. Bad hosts are reported and left out; ConfigError if unreadable."""
    data = load_yaml(path)
    extra = []
    unknown_keys({k: v for k, v in data.items() if k != "hosts"}, scaffold.cluster_schema(), "", CLUSTER_FREE, extra)
    host_schema = scaffold.cluster_schema()["hosts"]
    for raw in data.get("hosts") or []:
        if isinstance(raw, dict):
            unknown_keys(raw, host_schema, "hosts[{}]".format(raw.get("name", "?")), set(), extra)
    for key in extra:
        problems.warn("{}: unknown key {}".format(os.path.basename(path), key))

    ssh = deep_merge(SSH_DEFAULTS, data.get("ssh") or {})
    network = data.get("network") or {}
    defaults = data.get("defaults") or {}
    ib_count = dig(network, "expect.cx8.count", 4)
    raw_hosts = data.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        problems.error("{}: hosts must be a non-empty list".format(path))
        raw_hosts = []
    hosts = []
    for i, raw in enumerate(raw_hosts):
        host = _parse_host(raw, defaults, i, problems, ib_count)
        if host:
            hosts.append(host)
    cluster = Cluster(path, ssh, network, hosts)
    check_cluster(cluster, problems)
    return cluster


def check_cluster(cluster, problems):
    seen = {}
    for h in cluster.hosts:
        if h.name in seen:
            problems.error("{}: duplicate host name".format(h.name))
        seen[h.name] = h
    slots = {}
    for h in cluster.hosts:
        key = (h.rack, h.tray)
        if key in slots:
            problems.error("{} and {}: both are {} tray {}".format(slots[key], h.name, h.rack, h.tray))
        slots[key] = h.name

    for h in cluster.hosts:
        own = [("mgmt_pci", h.plan.get("mgmt_pci")), ("storage_pci", h.plan.get("storage_pci"))] + \
            [("ib_pci[{}]".format(i), v) for i, v in enumerate(h.plan.get("ib_pci") or [])]
        seen_here = {}
        for field, v in own:
            if v and v in seen_here:
                problems.error("{}: {} and {} are the same PCI address {}".format(h.name, seen_here[v], field, v))
            elif v:
                seen_here[v] = field

    owners = {}
    for h in cluster.hosts:
        addrs = [("ssh", h.ssh)]
        for field, iface in [("mgmt", h.mgmt), ("storage", h.storage)] + \
                [("ib[{}]".format(i), v) for i, v in enumerate(h.ib)]:
            if iface is not None:
                addrs.append((field, str(iface.ip)))
        for field, addr in addrs:
            if addr in owners:
                problems.error("address {} is used by {} and {}.{}".format(addr, owners[addr], h.name, field))
            else:
                owners[addr] = "{}.{}".format(h.name, field)

    rails = {}
    for h in cluster.hosts:
        for i, iface in enumerate(h.ib):
            if iface is not None:
                rails.setdefault(i, {}).setdefault(str(iface.network), []).append(h.name)
    for i, nets in sorted(rails.items()):
        if len(nets) > 1:
            detail = "; ".join("{} ({} hosts, e.g. {})".format(n, len(v), v[0]) for n, v in nets.items())
            problems.error("rail {}: IPoIB addresses are in different subnets: {}".format(i, detail))
    by_net = {}
    for i, nets in rails.items():
        for net in nets:
            by_net.setdefault(net, []).append(i)
    for net, idx in by_net.items():
        if len(idx) > 1:
            problems.warn("rails {} share subnet {}; each rail usually has its own".format(idx, net))

    for role in ("mgmt", "storage"):
        default = dig(cluster.network, role + ".gateway")
        if default:
            try:
                ipaddress.ip_address(str(default))
            except ValueError:
                problems.error("network.{}.gateway '{}' is not a valid address".format(role, default))
                default = None
        for h in cluster.hosts:
            addr = getattr(h, role)
            gw = h.plan.get(role + "_gateway") or default
            if addr is not None and gw and ipaddress.ip_address(str(gw)) not in addr.network:
                problems.error("{}: {} {} is not in the same subnet as gateway {}".format(h.name, role, addr, gw))

    net = cluster.network or {}
    if net.get("match_by", "mac") not in ("mac", "name"):
        problems.error("network.match_by must be mac or name")
    if str(dig(net, "ipoib.pkey", "0x7fff")).lower() != "0x7fff":
        problems.error("network.ipoib.pkey: only the default partition 0x7fff is supported")
    if dig(net, "ib_mode.reboot", "ask") not in ("ask", "auto", "never"):
        problems.error("network.ib_mode.reboot must be ask, auto or never")
    if not isinstance(net.get("require_pci", True), bool):
        problems.error("network.require_pci must be true or false")
    elif net.get("require_pci", True):
        need = [h.name for h in cluster.hosts if (h.mgmt is not None and not h.plan.get("mgmt_pci"))
                or (h.storage is not None and not h.plan.get("storage_pci"))
                or ((h.ib or h.plan.get("ib_mtu") or dig(net, "ipoib.mtu")) and not all(h.plan.get("ib_pci") or [""]))]
        if need:
            problems.warn("{} host(s) lack mgmt_pci / storage_pci / ib_pci (e.g. {}); 'mtest net apply' refuses them "
                          "while network.require_pci is true".format(len(need), need[0]))
    rb = net.get("rollback_seconds", 120)
    if not isinstance(rb, int) or isinstance(rb, bool) or rb < 30:
        problems.error("network.rollback_seconds must be an integer >= 30")
    for key in ("mgmt", "storage"):
        port = dig(net, key + ".port")
        if port is not None and port not in (0, 1):
            problems.error("network.{}.port must be 0 or 1 (BF3 port)".format(key))
    for net_cidr in (dig(net, "lom.pin_routes") or []) + (dig(net, "storage.routes") or []):
        try:
            ipaddress.ip_network(str(net_cidr))
        except ValueError:
            problems.error("network: '{}' is not a valid subnet".format(net_cidr))

    golden = [h.name for h in cluster.hosts if h.golden]
    if len(golden) > 1:
        problems.error("more than one golden host: {}".format(", ".join(golden)))
    elif not golden and cluster.hosts:
        problems.warn("no host is tagged golden; S3 cannot compare NIC mappings")


class Tests:

    def __init__(self, path, data):
        self.path = path
        self.data = data
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
    data = load_yaml(path)
    extra = []
    unknown_keys(data, scaffold.tests_schema(), "", TESTS_FREE, extra)
    for key in extra:
        problems.warn("{}: unknown key {}".format(os.path.basename(path), key))
    return Tests(path, data)


def load_both(cluster_path, tests_path, need_tests=True):
    """Load both files; returns (cluster, tests, problems). Missing tests.yaml is fine if not needed."""
    problems = Problems()
    cluster = load_cluster(cluster_path, problems)
    tests = None
    if need_tests or os.path.isfile(tests_path):
        try:
            tests = load_tests_file(tests_path, problems)
        except ConfigError as e:
            if need_tests:
                raise
            problems.warn(str(e))
    return cluster, tests, problems
