"""S4: configure the mgmt and storage ports, IPoIB and LOM policy routing with netplan, then verify.

Per host: render /etc/netplan/90-static-mgmt-storage.yaml (mgmt, storage, LOM policy routing) and
90-static-compute.yaml (IPoIB) from cluster.yaml + S3 data, move the LOM default route of the
existing LOM netplan file into the policy table, apply them behind a systemd rollback timer, reconnect over
the LOM, run the critical checks (replies to the control machine leave via the LOM, default route,
gateway ping) and only then cancel the rollback. After all hosts are done, the full checks
(addresses, MTU, IB ports, pings to a peer in the same rack) run.
"""

import difflib
import fnmatch
import ipaddress
import os
import sys
import time

import yaml

from mtest import profiles
from mtest.executor import pmap
from mtest.nic import STRUCTURAL
from mtest.nodecmd import NodeError, run_script
from mtest.ssh import Remote
from mtest.state import HostLocks, StateStore, load_nic, new_run_id, now_iso, write_json
from mtest.util import dig

ITEM = "network"
# mgmt + storage (+ the LOM policy routing) in one file, the compute rails in another.
FILES = {"mgmt_storage": "90-static-mgmt-storage.yaml", "compute": "90-static-compute.yaml"}
CRITICAL = ("route_control", "default_route", "ping:mgmt-gateway", "iface:mgmt")


class NetError(Exception):
    pass


def find_lom_id(files, lom):
    """(file, id, entry) of the netplan ethernets entry that configures the LOM port, or Nones."""
    name, mac = lom.get("if", ""), (lom.get("mac") or "").lower()
    for fname in sorted(files):
        try:
            data = yaml.safe_load(files[fname]) or {}
        except yaml.YAMLError:
            continue
        eths = dig(data, "network.ethernets") or {}
        for key, cfg in eths.items():
            cfg = cfg or {}
            match = cfg.get("match") or {}
            if match:
                if mac and str(match.get("macaddress", "")).lower() == mac:
                    return fname, key, cfg
                if match.get("name") and fnmatch.fnmatch(name, str(match["name"])):
                    return fname, key, cfg
            elif key == name:
                return fname, key, cfg
    return None, None, None


def conflicts(files, macs, names):
    """Other netplan entries that already configure the BF3 ports or IPoIB interfaces."""
    out = []
    macs = {m.lower() for m in macs if m}
    for fname in sorted(files):
        try:
            data = yaml.safe_load(files[fname]) or {}
        except yaml.YAMLError as e:
            out.append("{}: invalid YAML ({})".format(fname, e))
            continue
        for section in ("ethernets", "bonds", "vlans", "bridges"):
            for key, cfg in (dig(data, "network." + section) or {}).items():
                cfg = cfg or {}
                match = cfg.get("match") or {}
                mac = str(match.get("macaddress", "")).lower()
                pat = match.get("name")
                hit = (mac and mac in macs) or (not match and key in names) or \
                      (pat and any(fnmatch.fnmatch(n, str(pat)) for n in names))
                if hit:
                    out.append("{}: {}.{} already matches a port mtest configures".format(fname, section, key))
    return out


def _routes(nets, via):
    return [{"to": n, "via": via} if via else {"to": n, "scope": "link"} for n in nets]


def _guid(value):
    return str(value or "").lower().replace("0x", "").replace(":", "")


def _devices(nic):
    """PCI address -> discovered port (BF3 ports, plain NICs and CX8 rails) from the S3 record."""
    raw = nic.get("raw") or {}
    out = {}
    for p in raw.get("nics") or []:
        out[p["pci"]] = dict(p, kind="nic")
    for p in (raw.get("bf3") or {}).get("pfs") or []:
        out[p["pci"]] = dict(p, kind="bf3")
    for r in raw.get("rails") or nic.get("rails") or []:
        out[r["pci"]] = dict(r, kind="cx8")
    return out


def resolve_ports(host, nic, network):
    """Which local interface each planned address goes to, found by the PCI address in the plan.

    Returns {"mgmt", "storage": {netdev, mac, pci} or None, "rails": [{rail, netdev, ibdev, pci, addr, mtu}],
    "mgmt_gateway", "storage_gateway", "storage_routes", "lom_gateway"}; raises NetError when the plan
    and the host do not agree. mgmt is None when it is not configured by S4 (no address, or device lom).
    """
    plan = host.plan
    require = network.get("require_pci", True)
    devs = _devices(nic)
    found_bf3 = ", ".join(sorted(k for k, v in devs.items() if v["kind"] == "bf3")) or "none"
    found_cx8 = ", ".join(sorted(k for k, v in devs.items() if v["kind"] == "cx8")) or "none"

    def role_port(role, fallback_if, fallback_mac):
        kind = profiles.device(network, role)
        pci = plan.get(role + "_pci")
        if pci:
            d = devs.get(pci)
            if kind == "bf3" and (d is None or d["kind"] != "bf3"):
                raise NetError("{}_pci {} is not a BF3 port on this host (BF3 ports: {})".format(role, pci, found_bf3))
            if kind == "nic" and (d is None or d["kind"] == "cx8"):
                raise NetError("{}_pci {} is not a network port found on this host by S3{}".format(
                    role, pci, "" if d is None else " (it is a compute rail)"))
            port = {"netdev": d.get("netdev", ""), "mac": (d.get("mac") or "").lower(), "pci": pci}
        elif kind == "nic":
            raise NetError("{}_pci is empty in the IP plan (network.{}.device is nic)".format(role, role))
        elif require:
            raise NetError("{}_pci is empty in the IP plan (network.require_pci is true)".format(role))
        else:
            port = {"netdev": nic.get(fallback_if, ""), "mac": (nic.get(fallback_mac) or "").lower(), "pci": ""}
        if not port["netdev"] or not port["mac"]:
            raise NetError("{} port {} has no network interface".format(role, port["pci"] or "(S3 order)"))
        want = plan.get(role + "_mac")
        if want and want != port["mac"]:
            raise NetError("{} port {}: MAC {} differs from the IP plan {}".format(role, port["pci"] or port["netdev"],
                                                                                 port["mac"], want))
        return port

    out = {"mgmt": role_port("mgmt", "mgmt_if", "mgmt_mac")
           if host.mgmt is not None and profiles.device(network, "mgmt") != "lom" else None,
           "storage": role_port("storage", "storage_if", "storage_mac") if host.storage is not None else None,
           "rails": []}
    ib_mtu = plan.get("ib_mtu") or dig(network, "ipoib.mtu")
    pcis = plan.get("ib_pci") or []
    guids = plan.get("ib_guid") or []
    rails = nic.get("rails") or []
    for i in range(max(len(host.ib), len(pcis), len(rails))):
        addr = host.ib[i] if i < len(host.ib) else None
        pci = pcis[i] if i < len(pcis) else ""
        if addr is None and not ib_mtu:
            continue
        if pci:
            d = devs.get(pci)
            if d is None or d["kind"] != "cx8":
                raise NetError("ib_pci[{}] {} is not a compute NIC on this host (found: {})".format(i, pci, found_cx8))
        elif require:
            raise NetError("ib_pci[{}] is empty in the IP plan (needed to set the IB {})".format(
                i, "address" if addr is not None else "MTU"))
        elif i < len(rails):
            d = rails[i]
        else:
            continue
        if i < len(guids) and guids[i] and _guid(d.get("guid")) and _guid(guids[i]) != _guid(d.get("guid")):
            raise NetError("ib_pci[{}] {}: GUID {} differs from the IP plan {}".format(i, pci, d.get("guid"), guids[i]))
        if not d.get("netdev"):
            raise NetError("rail {} ({}) has no IPoIB interface".format(i, d.get("pci")))
        out["rails"].append({"rail": i, "netdev": d["netdev"], "ibdev": d.get("ibdev", ""), "pci": d.get("pci", ""),
                             "addr": addr, "mtu": ib_mtu})
    mgmt = network.get("mgmt") or {}
    storage = network.get("storage") or {}
    out["mgmt_gateway"] = plan.get("mgmt_gateway") or mgmt.get("gateway")
    out["storage_gateway"] = plan.get("storage_gateway") or storage.get("gateway")
    out["storage_routes"] = plan.get("storage_routes") or storage.get("routes") or []
    out["lom_gateway"] = plan.get("lom_gateway") or (nic.get("lom") or {}).get("gateway")
    return out


def _mgmt_default(host, network, ports):
    """True when S4 moves the default route from the LOM to the mgmt port."""
    return bool((network.get("mgmt") or {}).get("default_route", True) and ports["mgmt"] is not None
                and ports["mgmt_gateway"])


def _is_default(route):
    return str((route or {}).get("to", "")) in ("default", "0.0.0.0/0") and not (route or {}).get("table")


def move_lom_default(text, lom_id, table):
    """New text of the netplan file that configures the LOM, with its main-table default route
    (gateway4 or a "to: default" route) moved into the policy table; None when there is none.
    YAML comments of the file are not kept."""
    data = yaml.safe_load(text)
    entry = data["network"]["ethernets"][lom_id]
    moved = False
    routes = []
    if entry.get("gateway4"):
        routes.append({"to": "default", "via": entry.pop("gateway4"), "table": table})
        moved = True
    for r in entry.get("routes") or []:
        if _is_default(r):
            r = dict(r, to="default", table=table)
            moved = True
        routes.append(r)
    if not moved:
        return None
    entry["routes"] = routes
    head = "# mtest (S4) moved the default route of {} into routing table {}.\n".format(lom_id, table)
    return head + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def lom_policy(nic, network, ports, host_files):
    """(entry merged into the LOM id, {file: new text}) when the default route moves to mgmt.

    Replies from the LOM address use policy table lom.table (LOM subnet + default via the LOM
    gateway), so every peer that reaches the host over the LOM, the control machine included,
    keeps working. A default route the LOM file puts in the main table is moved into that table:
    netplan cannot remove it from another file.
    """
    info = nic.get("lom") or {}
    name = info.get("if") or "?"
    fname, lom_id, entry = find_lom_id(host_files, info)
    if not lom_id:
        raise NetError("the LOM port {} is not configured by netplan; refusing to change routes".format(name))
    entry = entry or {}
    if entry.get("dhcp4"):
        raise NetError("{}: the LOM entry {} uses DHCP; mtest expects a static LOM".format(fname, lom_id))
    try:
        iface = ipaddress.ip_interface(info.get("prefix") or "")
    except ValueError:
        raise NetError("the address of the LOM port {} is not known; run mtest nic discover again".format(name))
    if str(iface) not in [str(a) for a in entry.get("addresses") or []]:
        raise NetError("{}: the LOM entry {} does not configure the address {} mtest connects to".format(
            fname, lom_id, iface))
    cfg = network.get("lom") or {}
    table, priority = int(cfg.get("table", 100)), int(cfg.get("priority", 100))
    new_text = move_lom_default(host_files[fname], lom_id, table)
    edits = {fname: new_text} if new_text is not None else {}
    lom_routes = yaml.safe_load(new_text)["network"]["ethernets"][lom_id].get("routes") if new_text else \
        entry.get("routes")
    routes = [{"to": str(iface.network), "scope": "link", "table": table}]
    if not any(str(r.get("to")) in ("default", "0.0.0.0/0") and r.get("table") == table for r in lom_routes or []):
        gw = ports["lom_gateway"]
        if not gw:
            raise NetError("no gateway known for the LOM port {} (S3 or lom_gateway)".format(name))
        if ipaddress.ip_address(gw) not in iface.network:
            raise NetError("LOM gateway {} is not in the LOM subnet {}".format(gw, iface.network))
        routes.append({"to": "default", "via": gw, "table": table})
    e = {"routes": routes,
         "routing-policy": [{"from": "{}/32".format(iface.ip), "table": table, "priority": priority}]}
    return e, edits


def _doc(eth):
    head = "# Written by mtest (S4). Changes here are overwritten by 'mtest net apply'.\n"
    return head + yaml.safe_dump({"network": {"version": 2, "ethernets": eth}}, sort_keys=False,
                                 default_flow_style=False)


def render(host, nic, network, host_files):
    """(own, edits) for one host: {file: text} of the mtest netplan files (a network with nothing to
    set has no file), and {file: new text} of other netplan files to change (the LOM default route).

    host_files = the other netplan files on the host. The LOM settings reuse the id of the existing
    LOM entry so netplan merges them into it; a second entry for the same port would be ignored.
    """
    ports = resolve_ports(host, nic, network)
    mgmt = network.get("mgmt") or {}
    storage = network.get("storage") or {}
    by_name = network.get("match_by", "mac") == "name"

    def match(port):
        return {"name": port["netdev"]} if by_name else {"macaddress": port["mac"]}

    own, edits, eth = {}, {}, {}
    mgmt_default = _mgmt_default(host, network, ports)
    if ports["mgmt"]:
        e = {"match": match(ports["mgmt"]), "dhcp4": False, "dhcp6": False, "addresses": [str(host.mgmt)]}
        if mgmt.get("mtu"):
            e["mtu"] = int(mgmt["mtu"])
        routes = []
        if mgmt_default:
            routes.append({"to": "default", "via": ports["mgmt_gateway"]})
        routes += _routes(mgmt.get("routes") or [], ports["mgmt_gateway"])
        if routes:
            e["routes"] = routes
        if mgmt.get("dns"):
            e["nameservers"] = {"addresses": [str(x) for x in mgmt["dns"]]}
        eth["mtest-mgmt"] = e
    if ports["storage"]:
        e = {"match": match(ports["storage"]), "dhcp4": False, "dhcp6": False, "addresses": [str(host.storage)]}
        if storage.get("mtu"):
            e["mtu"] = int(storage["mtu"])
        if ports["storage_routes"]:
            e["routes"] = _routes(ports["storage_routes"], ports["storage_gateway"])
        eth["mtest-storage"] = e
    if mgmt_default:
        lom_entry, edits = lom_policy(nic, network, ports, host_files)
        eth[find_lom_id(host_files, nic.get("lom") or {})[1]] = lom_entry
    if eth:
        own[FILES["mgmt_storage"]] = _doc(eth)
    eth = {}
    for r in ports["rails"]:
        e = {"dhcp4": False, "dhcp6": False}
        if r["addr"] is not None:
            e["addresses"] = [str(r["addr"])]
        else:
            e["link-local"] = []
        if r["mtu"]:
            e["mtu"] = int(r["mtu"])
        eth[r["netdev"]] = e
    if eth:
        own[FILES["compute"]] = _doc(eth)
    return own, edits


def link_type_actions(nic, network):
    """(to_set, pending): rails whose LINK_TYPE is not IB, and rails waiting for a reboot."""
    if not dig(network, "ib_mode.enforce", True) or not nic.get("mlxconfig"):
        return [], []
    to_set, pending = [], []
    for r in nic.get("rails", []):
        cur, nxt = (r.get("link_type") or "").upper(), (r.get("link_type_next") or "").upper()
        if not cur or cur.startswith("IB"):
            continue
        (pending if nxt.startswith("IB") else to_set).append(r)
    return to_set, pending


def verify_args(host, nic, network, peer=None, critical_only=False):
    """Arguments of the node-side verify; peer = {"host": Host, "nic": record} of the ping partner."""
    ports = resolve_ports(host, nic, network)
    mgmt = network.get("mgmt") or {}
    storage = network.get("storage") or {}
    verify = network.get("verify") or {}
    lom = nic.get("lom") or {}
    args = {"action": "verify", "control_ip": lom.get("control_ip"), "lom_if": lom.get("if"), "ifaces": [],
            "ib": [], "pings": []}
    if _mgmt_default(host, network, ports):
        args["default_if"] = ports["mgmt"]["netdev"]
        # Replies to the control machine are routed by the source address (LOM policy table).
        args["control_src"] = lom.get("ip")
    if ports["mgmt"]:
        args["ifaces"].append({"label": "mgmt", "name": ports["mgmt"]["netdev"], "addr": str(host.mgmt),
                               "mtu": mgmt.get("mtu")})
        if verify.get("ping_gateway", True) and ports["mgmt_gateway"]:
            args["pings"].append({"label": "mgmt-gateway", "iface": ports["mgmt"]["netdev"],
                                  "target": ports["mgmt_gateway"]})
    if critical_only:
        return args
    if ports["storage"]:
        args["ifaces"].append({"label": "storage", "name": ports["storage"]["netdev"], "addr": str(host.storage),
                               "mtu": storage.get("mtu")})
    for r in ports["rails"]:
        args["ifaces"].append({"label": "ib{}".format(r["rail"]), "name": r["netdev"],
                               "addr": str(r["addr"]) if r["addr"] is not None else None, "mtu": r["mtu"]})
    rate = dig(network, "expect.cx8.rate_gbps")
    for r in nic.get("rails") or []:
        if r.get("ibdev"):
            args["ib"].append({"label": "rail{}".format(r["rail"]), "ibdev": r["ibdev"], "min_rate": rate})
    if peer is not None and verify.get("ping_peer_in_rack", True):
        ph = peer["host"]
        if ports["mgmt"] and ph.mgmt is not None:
            args["pings"].append({"label": "mgmt-peer", "iface": ports["mgmt"]["netdev"], "target": ph.mgmt_ip})
        if ports["storage"] and ph.storage is not None:
            args["pings"].append({"label": "storage-peer", "iface": ports["storage"]["netdev"],
                                  "target": str(ph.storage.ip)})
        for r in ports["rails"]:
            i = r["rail"]
            if r["addr"] is not None and i < len(ph.ib) and ph.ib[i] is not None:
                args["pings"].append({"label": "ib{}-peer".format(i), "iface": r["netdev"], "target": str(ph.ib[i].ip)})
    return args


def peers(hosts):
    """Peer of each host for ping checks: the next host (by tray) in the same rack, else any other host."""
    out = {}
    by_rack = {}
    for h in hosts:
        by_rack.setdefault(h.rack, []).append(h)
    for rack, members in by_rack.items():
        for i, h in enumerate(members):
            if len(members) > 1:
                out[h.name] = members[(i + 1) % len(members)]
            else:
                others = [x for x in hosts if x.name != h.name]
                out[h.name] = others[0] if others else None
    return out


class HostNet:
    """Everything known about one host during a net apply."""

    def __init__(self, host, nic):
        self.host = host
        self.nic = nic
        self.status = None
        self.reason = ""
        self.actions = []
        self.content = None
        self.current = None
        self.checks = []
        self.reboot_needed = False
        self.edits = {}             # other netplan files to rewrite: {name: new text}
        self.originals = {}         # their current text

    def changed(self):
        return self.content != self.current or bool(self.edits)

    def fail(self, status, reason):
        if self.status is None:
            self.status, self.reason = status, reason


class NetRun:

    def __init__(self, cluster, tests, hosts, out=print, dry_run=False, assume_yes=False, interactive=None):
        self.cluster = cluster
        self.tests = tests
        self.network = cluster.network or {}
        self.remote = Remote(cluster.ssh)
        self.parallel = int(cluster.ssh.get("parallel", 24))
        self.out = out
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.rollback = int(self.network.get("rollback_seconds", 120))
        self.items = [HostNet(h, load_nic(tests.results_dir, h.name)) for h in hosts]

    def node(self, hn, args, timeout=300):
        return run_script(self.remote, hn.host, "net_node.py", args, timeout=timeout)

    # ---------- plan ----------
    def prepare(self, hn):
        if hn.nic is None:
            return hn.fail("error", "no NIC data; run mtest nic discover first")
        bad = ["{}: {}".format(k, v["reason"]) for k, v in (hn.nic.get("checks") or {}).items()
               if k in STRUCTURAL and v["result"] == "fail"]
        if bad:
            return hn.fail("error", "S3 checks failed: " + "; ".join(bad))
        try:
            info = self.node(hn, {"action": "inspect"})
        except NodeError as e:
            return hn.fail("error", str(e))
        if not info.get("netplan") or not info.get("systemd_run"):
            return hn.fail("error", "netplan and systemd-run are required on the host")
        if info.get("rollback_active"):
            return hn.fail("error", "an earlier mtest change is still waiting for confirmation or rollback")
        files = info["files"]
        try:
            ports = resolve_ports(hn.host, hn.nic, self.network)
        except NetError as e:
            return hn.fail("error", str(e))
        bf3 = [p for p in (ports["mgmt"], ports["storage"]) if p]
        names = [p["netdev"] for p in bf3] + [r["netdev"] for r in ports["rails"]]
        found = conflicts(files, [p["mac"] for p in bf3], names)
        if found:
            return hn.fail("error", "; ".join(found))
        try:
            hn.content, hn.edits = render(hn.host, hn.nic, self.network, files)
        except NetError as e:
            return hn.fail("error", str(e))
        hn.current = info.get("own") or {}
        hn.originals = {name: files[name] for name in hn.edits}
        to_set, pending = link_type_actions(hn.nic, self.network)
        if to_set:
            hn.actions.append("set LINK_TYPE_P1=IB on " + ", ".join(r["pci"] for r in to_set))
        if to_set or pending:
            hn.reboot_needed = True
            hn.actions.append("reboot to apply the IB link type ({})".format(
                ", ".join("rail{}".format(r["rail"]) for r in to_set + pending)))
        hn.to_set = to_set
        if hn.changed():
            changed = sorted(n for n in set(hn.content) | set(hn.current) if hn.content.get(n) != hn.current.get(n))
            changed += sorted(hn.edits)
            if hn.edits:
                hn.actions.append("move the LOM default route in {} into table {}".format(
                    ", ".join(sorted(hn.edits)), dig(self.network, "lom.table", 100)))
            hn.actions.append("write /etc/netplan/{{{}}} and netplan apply (rollback after {}s)".format(
                ",".join(changed), self.rollback))

    def diff(self, hn):
        out = []
        old_files = dict(hn.current or {}, **hn.originals)
        new_files = dict(hn.content or {}, **hn.edits)
        for name in sorted(set(old_files) | set(new_files)):
            old, new = old_files.get(name), new_files.get(name)
            out.append("".join(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True),
                                                    "a/" + name if old is not None else "/dev/null",
                                                    "b/" + name if new is not None else "/dev/null")))
        return "".join(out)

    # ---------- apply ----------
    def apply_one(self, hn):
        if hn.status is not None:
            return
        if getattr(hn, "to_set", None):
            try:
                res = self.node(hn, {"action": "set_link_type", "pcis": [r["pci"] for r in hn.to_set], "value": 1})
            except NodeError as e:
                return hn.fail("error", "mlxconfig: " + str(e))
            if not res.get("ok"):
                return hn.fail("error", "mlxconfig set failed: {}".format(res.get("devices")))
        if not hn.changed():
            return
        edits = {n: {"old": hn.originals[n], "new": t} for n, t in hn.edits.items()}
        try:
            res = self.node(hn, {"action": "apply", "files": hn.content, "edits": edits,
                                 "rollback_seconds": self.rollback})
        except NodeError as e:
            return hn.fail("error", "apply: " + str(e))
        if not res.get("ok"):
            return hn.fail("error", res.get("error", "apply failed"))
        applied = time.time()
        time.sleep(3)
        deadline = applied + max(10, self.rollback - 30)
        last = None
        while time.time() < deadline:
            try:
                res = self.node(hn, verify_args(hn.host, hn.nic, self.network, critical_only=True), timeout=60)
            except NodeError as e:
                last = str(e)
                time.sleep(3)
                continue
            critical = [c for c in res["checks"] if c["name"] in CRITICAL]
            last = "; ".join("{}: {}".format(c["name"], c["detail"]) for c in critical if c["result"] != "pass")
            if not last:
                try:
                    done = self.node(hn, {"action": "confirm"}, timeout=60)
                except NodeError as e:
                    last = "confirm: " + str(e)
                    break
                if done.get("ok"):
                    hn.checks = res["checks"]
                    return
                last = "checks passed too late, the rollback had already started"
                break
            time.sleep(3)
        self.out("  {}: critical checks failed ({}); waiting for the automatic rollback".format(hn.host.name, last))
        hn.fail("fail", "rolled back: " + (last or "host not reachable after apply"))
        self.wait_rollback(hn)

    def wait_rollback(self, hn):
        deadline = time.time() + self.rollback + 90
        while time.time() < deadline:
            time.sleep(5)
            try:
                info = self.node(hn, {"action": "inspect"}, timeout=30)
            except NodeError:
                continue
            if not info.get("rollback_active") and (info.get("own") or {}) == hn.current and \
                    all(info["files"].get(n) == t for n, t in hn.originals.items()):
                hn.reason += " (old configuration restored)"
                return
        hn.reason += " (rollback not confirmed: check the host console)"

    def reboot_one(self, hn):
        if hn.status is not None or not hn.reboot_needed:
            return
        policy = dig(self.network, "ib_mode.reboot", "ask")
        if policy == "never" or (policy == "ask" and not self.reboot_ok):
            return hn.fail("fail", "IB link type changes need a reboot (ib_mode.reboot: {})".format(policy))
        try:
            self.node(hn, {"action": "reboot"}, timeout=30)
        except NodeError:
            pass
        down_by = time.time() + 300
        while time.time() < down_by and self.remote.run(hn.host, "true", timeout=10).ok:
            time.sleep(5)
        up_by = time.time() + 1200
        while time.time() < up_by:
            time.sleep(10)
            if self.remote.run(hn.host, "true", timeout=15).ok:
                hn.actions.append("rebooted")
                return
        hn.fail("error", "host did not come back within 20 minutes after reboot")

    # ---------- main ----------
    def run(self):
        results = self.tests.results_dir
        names = [hn.host.name for hn in self.items]
        locks = HostLocks(results)
        if not self.dry_run:
            conflicts_ = locks.acquire(names, "net", ITEM)
            if conflicts_:
                for h, info in conflicts_.items():
                    self.out("error: {} is in use by run {}".format(h, info.get("run_id")))
                return 1
        try:
            return self._run(results)
        finally:
            if not self.dry_run:
                locks.release(names)

    def _run(self, results):
        pmap(self.prepare, self.items, self.parallel)
        for hn in self.items:
            if hn.status is not None:
                self.out("ERROR {:<20} {}".format(hn.host.name, hn.reason))
            elif not hn.actions:
                self.out("ok    {:<20} no change".format(hn.host.name))
            else:
                self.out("plan  {:<20} {}".format(hn.host.name, "; ".join(hn.actions)))
                if self.dry_run and hn.changed():
                    self.out(self.diff(hn))
        if self.dry_run:
            return 0 if all(hn.status is None for hn in self.items) else 1
        reboot_hosts = [hn.host.name for hn in self.items if hn.status is None and hn.reboot_needed]
        self.reboot_ok = self.assume_yes
        if reboot_hosts and dig(self.network, "ib_mode.reboot", "ask") == "ask" and not self.assume_yes:
            if self.interactive:
                ans = input("reboot {} host(s) to apply the IB link type ({})? [y/N] ".format(
                    len(reboot_hosts), ", ".join(reboot_hosts[:5]) + (" ..." if len(reboot_hosts) > 5 else "")))
                self.reboot_ok = ans.strip().lower() in ("y", "yes")
        run_id = new_run_id(results)
        started = now_iso()
        pmap(self.apply_one, self.items, self.parallel)
        pmap(self.reboot_one, self.items, self.parallel)
        good = [hn for hn in self.items if hn.status is None]
        peer_of = peers([hn.host for hn in good])
        by_name = {hn.host.name: hn for hn in good}

        def full(hn):
            p = peer_of.get(hn.host.name)
            peer = {"host": p, "nic": by_name[p.name].nic} if p is not None else None
            try:
                res = self.node(hn, verify_args(hn.host, hn.nic, self.network, peer=peer), timeout=180)
            except NodeError as e:
                return hn.fail("error", "verify: " + str(e))
            hn.checks = res["checks"]
            bad = [c for c in res["checks"] if c["result"] != "pass"]
            if bad:
                hn.fail("fail", "; ".join("{}: {}".format(c["name"], c["detail"]) for c in bad))
            else:
                hn.status = "pass"

        pmap(full, good, self.parallel)
        self.record(results, run_id, started)
        for hn in self.items:
            mark = "ok   " if hn.status == "pass" else hn.status.upper()
            self.out("{} {:<20} {}".format(mark, hn.host.name, hn.reason or "{} checks passed".format(len(hn.checks))))
        return 0 if all(hn.status == "pass" for hn in self.items) else 1

    def record(self, results, run_id, started):
        run_dir = os.path.join(results, run_id)
        entries = {}
        for hn in self.items:
            checks = {c["name"]: {"result": c["result"], "reason": c["detail"]} for c in hn.checks}
            summary = {"item": ITEM, "hostname": hn.host.name, "run_id": run_id, "status": hn.status,
                       "reason": hn.reason, "group": hn.host.name, "rank": 0, "group_result": False,
                       "started_at": started, "finished_at": now_iso(), "checks": checks,
                       "metrics": {"actions": {"value": "; ".join(hn.actions) or "no change"}}}
            write_json(os.path.join(run_dir, hn.host.name, ITEM, "summary.json"), summary)
            for name, text in sorted(dict(hn.content or {}, **hn.edits).items()):
                with open(os.path.join(run_dir, hn.host.name, ITEM, name), "w") as f:
                    f.write(text)
            entries[hn.host.name] = {ITEM: {"status": hn.status, "reason": hn.reason, "run_id": run_id,
                                            "group": hn.host.name, "rank": 0, "group_result": False,
                                            "finished_at": summary["finished_at"]}}
        StateStore(results).update(entries)
        status = "pass" if all(hn.status == "pass" for hn in self.items) else "fail"
        write_json(os.path.join(run_dir, "run.json"), {
            "run_id": run_id, "pid": os.getpid(), "status": status, "started_at": started, "finished_at": now_iso(),
            "items": {ITEM: {"status": status, "hosts": [hn.host.name for hn in self.items], "groups": {}}}})


def verify_only(cluster, tests, hosts, out=print):
    """mtest net verify: run the full checks without changing anything."""
    nr = NetRun(cluster, tests, hosts, out=out)
    nr.reboot_ok = False
    good = [hn for hn in nr.items if hn.nic is not None]
    for hn in nr.items:
        if hn.nic is None:
            hn.fail("error", "no NIC data; run mtest nic discover first")
    peer_of = peers([hn.host for hn in good])
    by_name = {hn.host.name: hn for hn in good}

    def one(hn):
        p = peer_of.get(hn.host.name)
        peer = {"host": p, "nic": by_name[p.name].nic} if p is not None else None
        try:
            res = nr.node(hn, verify_args(hn.host, hn.nic, nr.network, peer=peer), timeout=180)
        except NodeError as e:
            return hn.fail("error", str(e))
        hn.checks = res["checks"]
        bad = [c for c in res["checks"] if c["result"] != "pass"]
        if bad:
            hn.fail("fail", "; ".join("{}: {}".format(c["name"], c["detail"]) for c in bad))
        else:
            hn.status = "pass"

    pmap(one, good, nr.parallel)
    for hn in nr.items:
        out("{} {:<20} {}".format("ok   " if hn.status == "pass" else hn.status.upper(), hn.host.name,
                                   hn.reason or "{} checks passed".format(len(hn.checks))))
    return 0 if all(hn.status == "pass" for hn in nr.items) else 1
