"""testkit net-check: compare the network of every host with network.yaml (read only).

network.yaml lists every host under "hosts", keyed by its short hostname; each entry is complete
(nothing is inherited). The same file is read by the site script that configures the hosts.

  1. file checks: types, CIDR addresses, quoted PCI addresses, gateways and routes inside the
     subnet, duplicate addresses, rails in one subnet, and fields that differ from most hosts
  2. host checks over ssh as root (node_scripts/net_check.py): PCI -> interface, link layer and state,
     address, MTU, IPoIB pkey, default route, storage routes, LOM policy routing, DNS, and
     pings to the gateways and to the same rail of a peer in the same rack
"""

import ipaddress
import json
import re
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

from testkit.util import ConfigError, Problems, load_yaml, natural_key

NETS = ("lom", "mgmt", "storage", "compute")
TOP_KEYS = {"version", "hosts"}
HOST_KEYS = {"rack", "tray"} | set(NETS)
PORT_KEYS = {"pci", "ip", "mtu", "gateway", "default_route", "dns", "routes", "link_layer"}
NET_KEYS = {"lom": {"ip", "gateway", "policy"}, "mgmt": PORT_KEYS, "storage": PORT_KEYS,
            "compute": {"link_layer", "mtu", "pkey", "devices"}}
DEVICE_KEYS = {"pci", "ip", "mtu"}
LINK_LAYERS = ("ethernet", "infiniband")
PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")


class Port:
    """One interface testkit checks: mgmt, storage or one compute rail."""

    def __init__(self, label, net, pci, ip, mtu=None, link_layer="ethernet", pkey=None):
        self.label, self.net, self.pci, self.ip = label, net, pci, ip
        self.mtu, self.link_layer, self.pkey = mtu, link_layer, pkey


class Host:

    def __init__(self, name, rack=None, tray=None):
        self.name = name
        self.rack = rack
        self.tray = tray
        self.ssh = None                 # LOM address without the prefix; used by Remote
        self.lom = {}
        self.ports = []
        self.default = None             # (label, gateway)
        self.routes = []                # (label, to, via)
        self.dns = []                   # (label, [servers])
        self.gateways = OrderedDict()   # label -> gateway to ping
        self.addrs = []                 # (label, CIDR) of every parsed address, for the duplicate check
        self.fields = OrderedDict()     # comparable settings, for the consistency check
        self.valid = True

    def rails(self):
        return [p for p in self.ports if p.net == "compute"]


class Network:

    def __init__(self, path, hosts):
        self.path, self.hosts = path, hosts


def _free(key):
    return isinstance(key, str) and key.startswith("x-")


def _unknown(data, allowed, where, p):
    for key in data:
        if key not in allowed and not _free(key):
            p.warn("{}: unknown key {}".format(where, key))


def _iface(value, where, p):
    if not isinstance(value, str) or "/" not in value:
        p.error("{}: {!r} must be an address with prefix (CIDR), e.g. 10.20.1.11/24".format(where, value))
        return None
    try:
        return ipaddress.ip_interface(value)
    except ValueError:
        p.error("{}: {!r} is not a valid address".format(where, value))
        return None


def _addr(value, where, p):
    try:
        return ipaddress.ip_address(str(value))
    except ValueError:
        p.error("{}: {!r} is not a valid IP address".format(where, value))
        return None


def _pci(value, where, p):
    if not isinstance(value, str):
        p.error('{}: PCI address must be quoted, e.g. "0000:03:00.0" (YAML reads {!r} as a number)'.format(
            where, value))
        return None
    value = value.strip().lower()
    if not PCI_RE.match(value):
        p.error("{}: {!r} is not a PCI address like 0000:03:00.0".format(where, value))
        return None
    return value


def _int(value, where, p, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        p.error("{}: {!r} must be an integer {}..{}".format(where, value, low, high))
        return None
    return value


def _link_layer(value, where, p):
    value = str(value or "ethernet").lower()
    if value not in LINK_LAYERS:
        p.error("{}: link_layer {!r} must be one of {}".format(where, value, ", ".join(LINK_LAYERS)))
        return "ethernet"
    return value


def _gateway(value, iface, where, p):
    if value is None:
        return None
    gw = _addr(value, where, p)
    if gw is not None and iface is not None and gw not in iface.network:
        p.error("{}: gateway {} is outside {}".format(where, gw, iface.network))
    return gw


def _mapping(value, where, p):
    if not isinstance(value, dict):
        p.error("{}: must be a mapping".format(where))
        return None
    return value


def _parse_lom(host, data, where, p):
    _unknown(data, NET_KEYS["lom"], where, p)
    if data.get("ip") is None:
        p.error("{}.ip is required (testkit logs in to this address)".format(where))
        return
    iface = _iface(data["ip"], where + ".ip", p)
    if iface is None:
        return
    host.ssh = str(iface.ip)
    host.addrs.append(("lom", str(iface)))
    host.lom = {"ip": str(iface), "gateway": None, "policy": None}
    gw = _gateway(data.get("gateway"), iface, where + ".gateway", p)
    if gw is not None:
        host.lom["gateway"] = str(gw)
    policy = data.get("policy")
    if policy is not None and _mapping(policy, where + ".policy", p) is not None:
        _unknown(policy, {"table", "priority"}, where + ".policy", p)
        table = _int(policy.get("table"), where + ".policy.table", p, 1, 2 ** 32 - 1)
        prio = _int(policy.get("priority"), where + ".policy.priority", p, 0, 2 ** 32 - 1)
        if table in (253, 254, 255):
            p.error("{}.policy.table: {} is a reserved table (default/main/local)".format(where, table))
        if table is not None and prio is not None:
            host.lom["policy"] = {"table": table, "priority": prio}
    host.fields["lom.prefix"] = iface.network.prefixlen
    host.fields["lom.gateway"] = host.lom["gateway"]
    host.fields["lom.policy"] = host.lom["policy"]


def _parse_port(host, net, data, where, p):
    _unknown(data, NET_KEYS[net], where, p)
    for key in ("pci", "ip"):
        if data.get(key) is None:
            p.error("{}.{} is required".format(where, key))
    pci = _pci(data.get("pci"), where + ".pci", p) if data.get("pci") is not None else None
    iface = _iface(data.get("ip"), where + ".ip", p) if data.get("ip") is not None else None
    mtu = _int(data["mtu"], where + ".mtu", p, 68, 65520) if data.get("mtu") is not None else None
    ll = _link_layer(data.get("link_layer"), where + ".link_layer", p)
    gw = _gateway(data.get("gateway"), iface, where + ".gateway", p)
    if iface is not None:
        host.addrs.append((net, str(iface)))
    if pci is None or iface is None:
        return
    host.ports.append(Port(net, net, pci, str(iface), mtu, ll))
    if gw is not None:
        host.gateways[net] = str(gw)
    if data.get("default_route"):
        if gw is None:
            p.error("{}.default_route needs {}.gateway".format(where, where))
        elif host.default:
            p.error("{}.default_route: {} already has the default route".format(where, host.default[0]))
        else:
            host.default = (net, str(gw))
    routes = data.get("routes") or []
    if not isinstance(routes, list):
        p.error("{}.routes: must be a list of {{to, via}}".format(where))
        routes = []
    norm = []
    for i, r in enumerate(routes):
        rw = "{}.routes[{}]".format(where, i)
        if _mapping(r, rw, p) is None:
            continue
        _unknown(r, {"to", "via"}, rw, p)
        try:
            to = ipaddress.ip_network(str(r.get("to")), strict=False)
        except ValueError:
            p.error("{}.to: {!r} is not a network".format(rw, r.get("to")))
            continue
        if r.get("via") is None:
            p.error("{}.via is required".format(rw))
            continue
        via = _gateway(r.get("via"), iface, rw + ".via", p)
        if via is None:
            continue
        host.routes.append((net, str(to), str(via)))
        norm.append("{} via {}".format(to, via))
        host.gateways.setdefault(net, str(via))
    dns = data.get("dns") or []
    if not isinstance(dns, list):
        p.error("{}.dns: must be a list of addresses".format(where))
        dns = []
    servers = [str(a) for a in (_addr(d, where + ".dns", p) for d in dns) if a is not None]
    if servers:
        host.dns.append((net, servers))
    for key, value in (("pci", pci), ("prefix", iface.network.prefixlen), ("mtu", mtu), ("link_layer", ll),
                       ("gateway", str(gw) if gw else None), ("default_route", bool(data.get("default_route"))),
                       ("routes", norm), ("dns", servers)):
        host.fields["{}.{}".format(net, key)] = value


def _parse_compute(host, data, where, p):
    _unknown(data, NET_KEYS["compute"], where, p)
    ll = _link_layer(data.get("link_layer"), where + ".link_layer", p)
    mtu = _int(data["mtu"], where + ".mtu", p, 68, 65520) if data.get("mtu") is not None else None
    pkey = data.get("pkey")
    if pkey is not None:
        try:
            pkey = "0x{:04x}".format(pkey if isinstance(pkey, int) and not isinstance(pkey, bool)
                                     else int(str(pkey), 16))
        except ValueError:
            p.error("{}.pkey: {!r} is not a hex partition key like 0x7fff".format(where, pkey))
            pkey = None
        if pkey and ll != "infiniband":
            p.warn("{}.pkey is only used with link_layer: infiniband".format(where))
            pkey = None
    devices = data.get("devices")
    if not isinstance(devices, list) or not devices:
        p.error("{}.devices: must be a non-empty list of {{pci, ip}}".format(where))
        return
    host.fields.update([("compute.link_layer", ll), ("compute.mtu", mtu), ("compute.pkey", pkey),
                        ("compute.devices", len(devices))])
    for i, dev in enumerate(devices):
        dw = "{}.devices[{}]".format(where, i)
        if _mapping(dev, dw, p) is None:
            continue
        _unknown(dev, DEVICE_KEYS, dw, p)
        for key in ("pci", "ip"):
            if dev.get(key) is None:
                p.error("{}.{} is required".format(dw, key))
        pci = _pci(dev.get("pci"), dw + ".pci", p) if dev.get("pci") is not None else None
        iface = _iface(dev.get("ip"), dw + ".ip", p) if dev.get("ip") is not None else None
        dmtu = _int(dev["mtu"], dw + ".mtu", p, 68, 65520) if dev.get("mtu") is not None else mtu
        if iface is not None:
            host.addrs.append(("rail{}".format(i), str(iface)))
        host.fields["compute.devices[{}].pci".format(i)] = pci
        host.fields["compute.devices[{}].mtu".format(i)] = dmtu
        if pci is not None and iface is not None:
            host.ports.append(Port("rail{}".format(i), "compute", pci, str(iface), dmtu, ll, pkey))


def _parse_host(name, data, p):
    where = "hosts.{}".format(name)
    host = Host(str(name))
    if _mapping(data, where, p) is None:
        host.valid = False
        return host
    before = len(p.errors)
    _unknown(data, HOST_KEYS, where, p)
    host.rack = None if data.get("rack") is None else str(data["rack"])
    if data.get("tray") is not None:
        host.tray = _int(data["tray"], where + ".tray", p, 1, 10 ** 6)
    lom = data.get("lom")
    if lom is None:
        p.error("{}.lom is required (lom.ip is the ssh address)".format(where))
    elif _mapping(lom, where + ".lom", p) is not None:
        _parse_lom(host, lom, where + ".lom", p)
    for net in ("mgmt", "storage"):
        if data.get(net) is not None and _mapping(data[net], "{}.{}".format(where, net), p) is not None:
            _parse_port(host, net, data[net], "{}.{}".format(where, net), p)
    if data.get("compute") is not None and _mapping(data["compute"], where + ".compute", p) is not None:
        _parse_compute(host, data["compute"], where + ".compute", p)
    pcis = Counter(port.pci for port in host.ports)
    for pci, n in pcis.items():
        if n > 1:
            p.error("{}: PCI {} is used by {} ports".format(where, pci, n))
    host.valid = len(p.errors) == before and host.ssh is not None
    return host


def _cross_checks(hosts, p):
    owners = {}
    for h in hosts:
        for label, ip in h.addrs:
            owners.setdefault(ip.split("/")[0], []).append("{}.{}".format(h.name, label))
    for ip, users in owners.items():
        if len(users) > 1:
            p.error("address {} is used by {}".format(ip, ", ".join(users)))
    # Every host must reach the same rail of its peers: rail i is one subnet across the cluster.
    rails = {}
    for h in hosts:
        for port in h.rails():
            rails.setdefault(port.label, {}).setdefault(str(ipaddress.ip_interface(port.ip).network), []).append(h.name)
    for label, nets in sorted(rails.items(), key=lambda kv: natural_key(kv[0])):
        if len(nets) > 1:
            main = max(nets, key=lambda n: len(nets[n]))
            for net, names in sorted(nets.items()):
                if net != main:
                    p.error("compute {}: {} in {}, other hosts in {}".format(label, ", ".join(names), net, main))
    _consistency(hosts, p)


def _show(value):
    return "-" if value is None or value == [] else json.dumps(value) if isinstance(value, (list, dict)) else str(value)


def _consistency(hosts, p):
    """Warn about settings that differ from most hosts."""
    if len(hosts) < 3:
        return
    keys = []
    for h in hosts:
        keys += [k for k in h.fields if k not in keys]
    for key in keys:
        values = [(h, json.dumps(h.fields.get(key), sort_keys=True)) for h in hosts]
        counts = Counter(v for _, v in values)
        if len(counts) == 1:
            continue
        want = counts.most_common(1)[0][0]
        odd = [h for h, v in values if v != want]
        shown = ", ".join("{} {}".format(h.name, _show(h.fields.get(key))) for h in odd[:5])
        more = " and {} more".format(len(odd) - 5) if len(odd) > 5 else ""
        p.warn("{}: {}{} differs from most hosts ({})".format(key, shown, more, _show(json.loads(want))))


def load_network(path):
    """Read and check network.yaml; returns (Network, Problems). ConfigError if unreadable."""
    data = load_yaml(path)
    p = Problems()
    _unknown(data, TOP_KEYS, "network.yaml", p)
    if data.get("version", 1) != 1:
        p.error("version {!r} is not supported (expected 1)".format(data.get("version")))
    raw = data.get("hosts")
    if not isinstance(raw, dict) or not raw:
        p.error("hosts: must be a mapping of hostname -> host network")
        raw = {}
    hosts = [_parse_host(name, value, p) for name, value in raw.items()]
    _cross_checks(hosts, p)
    slots = {}
    for h in hosts:
        if h.rack is not None and h.tray is not None:
            if (h.rack, h.tray) in slots:
                p.error("hosts.{} and hosts.{}: both are rack {} tray {}".format(slots[(h.rack, h.tray)], h.name,
                                                                                h.rack, h.tray))
            slots.setdefault((h.rack, h.tray), h.name)
    return Network(path, hosts), p


def select(hosts, only=None, exclude=None):
    """--hosts / --exclude: comma-separated host names or racks; 'all' = every host."""

    def match(spec):
        out = set()
        for token in [t.strip() for t in spec.split(",") if t.strip()]:
            hit = {h.name for h in hosts if token in ("all", h.name, h.rack)}
            if not hit:
                raise ConfigError("--hosts/--exclude: '{}' matches no host or rack".format(token))
            out |= hit
        return out

    names = match(only) if only else {h.name for h in hosts}
    if exclude:
        names -= match(exclude)
    return [h for h in hosts if h.name in names]


def peer(host, hosts):
    """Next host (in name order) in the same rack (hosts without a rack form one group), or None."""
    group = sorted([h for h in hosts if h.rack == host.rack], key=lambda h: natural_key(h.name))
    if len(group) < 2:
        return None
    return group[(group.index(host) + 1) % len(group)]


def node_args(host, hosts, ping=True):
    args = {"hostname": host.name, "lom": host.lom,
            "ports": [{"label": port.label, "pci": port.pci, "ip": port.ip, "mtu": port.mtu,
                       "link_layer": port.link_layer, "pkey": port.pkey} for port in host.ports],
            "default": {"label": host.default[0], "gateway": host.default[1]} if host.default else None,
            "routes": [{"label": l, "to": to, "via": via} for l, to, via in host.routes],
            "dns": [{"label": l, "servers": s} for l, s in host.dns], "pings": []}
    if ping:
        for label, gw in host.gateways.items():
            args["pings"].append({"name": label + "-gw", "label": label, "target": gw})
        other = peer(host, hosts)
        if other is not None:
            theirs = {port.label: port for port in other.rails()}
            for port in host.rails():
                if port.label in theirs:
                    args["pings"].append({"name": "{}-peer".format(port.label), "label": port.label,
                                          "target": theirs[port.label].ip.split("/")[0]})
    return args


def check_hosts(network, hosts, ping=True):
    """Run the node checks on hosts in parallel; returns {name: {"ok", "checks"} or {"ok", "error"}}."""
    from testkit.nodecmd import NodeError, run_script
    from testkit.ssh import Remote, settings
    ssh = settings()
    remote = Remote(ssh)
    valid = [h for h in network.hosts if h.valid]

    def one(host):
        try:
            return host.name, run_script(remote, host, "net_check.py", node_args(host, valid, ping), timeout=180)
        except NodeError as e:
            return host.name, {"ok": False, "error": str(e), "checks": []}

    with ThreadPoolExecutor(max_workers=max(1, int(ssh["parallel"]))) as pool:
        return dict(pool.map(one, hosts))


def format_results(hosts, results):
    lines = []
    width = max([len(h.name) for h in hosts] + [4])
    for h in hosts:
        res = results[h.name]
        if res.get("error"):
            lines.append("{:<{w}}  {:<26}  {:<4}  {}".format(h.name, "ssh", "FAIL", res["error"], w=width))
            continue
        for c in res["checks"]:
            lines.append("{:<{w}}  {:<26}  {:<4}  {}".format(h.name, c["name"], c["result"].upper(), c["detail"],
                                                           w=width))
    return "\n".join(lines)


def run(path, only=None, exclude=None, ping=True, static=False, as_json=False, out=print):
    """testkit net-check. Exit code: 0 all pass, 1 a host check failed, 2 network.yaml has errors."""
    network, p = load_network(path)
    for msg in p.warnings:
        out("warning: " + msg)
    for msg in p.errors:
        out("error: " + msg)
    hosts = select(network.hosts, only, exclude)
    out("{}: {} hosts, {} selected; {} error(s), {} warning(s)".format(
        path, len(network.hosts), len(hosts), len(p.errors), len(p.warnings)))
    if not p.ok:
        return 2
    if static:
        return 0
    results = check_hosts(network, hosts, ping)
    if as_json:
        out(json.dumps(results, indent=2, sort_keys=True))
    else:
        out(format_results(hosts, results))
    bad = [h.name for h in hosts if not results[h.name]["ok"]]
    warn = [h.name for h in hosts if any(c["result"] == "warn" for c in results[h.name]["checks"])]
    out("net-check: {} hosts, {} pass, {} fail{}{}".format(
        len(hosts), len(hosts) - len(bad), len(bad), " ({})".format(", ".join(bad)) if bad else "",
        "; warnings on {}".format(", ".join(warn)) if warn else ""))
    return 1 if bad else 0
