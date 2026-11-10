"""S4: configure BF3 (mgmt, storage), IPoIB and the LOM pin routes with netplan, then verify.

Per host: render /etc/netplan/60-mtest.yaml from cluster.yaml + S3 data, apply it behind a
systemd rollback timer, reconnect over the LOM, run the critical checks (control route via
LOM, default route, gateway ping) and only then cancel the rollback. After all hosts are
done, the full checks (addresses, MTU, IB ports, pings to a peer in the same rack) run.
"""

import difflib
import fnmatch
import os
import sys
import time

import yaml

from mtest.executor import pmap
from mtest.nic import STRUCTURAL
from mtest.nodecmd import NodeError, run_script
from mtest.ssh import Remote
from mtest.state import HostLocks, StateStore, load_nic, new_run_id, now_iso, write_json
from mtest.util import dig

ITEM = "network"
OWN_FILE = "60-mtest.yaml"
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


def render(host, nic, network, lom_id, lom_dhcp=True):
    """Text of 60-mtest.yaml for one host.

    The LOM entry reuses the id of the existing netplan entry (lom_id) so netplan merges the
    pin routes into it; a second entry for the same port would be ignored by networkd.
    """
    mgmt = network.get("mgmt") or {}
    storage = network.get("storage") or {}
    ipoib = network.get("ipoib") or {}
    lom_cfg = network.get("lom") or {}
    eth = {}
    by_name = network.get("match_by", "mac") == "name"

    def match(mac_key, if_key):
        return {"name": nic[if_key]} if by_name else {"macaddress": nic[mac_key]}

    if host.mgmt is not None:
        if not nic.get("mgmt_mac"):
            raise NetError("BF3 port {} has no netdev (S3)".format(mgmt.get("port", 0)))
        e = {"match": match("mgmt_mac", "mgmt_if"), "dhcp4": False, "dhcp6": False,
             "addresses": [str(host.mgmt)]}
        if mgmt.get("mtu"):
            e["mtu"] = int(mgmt["mtu"])
        routes = []
        if mgmt.get("default_route", True) and mgmt.get("gateway"):
            routes.append({"to": "default", "via": str(mgmt["gateway"])})
        routes += _routes(mgmt.get("routes") or [], mgmt.get("gateway"))
        if routes:
            e["routes"] = routes
        if mgmt.get("dns"):
            e["nameservers"] = {"addresses": [str(x) for x in mgmt["dns"]]}
        eth["mtest-mgmt"] = e
    if host.storage is not None:
        if not nic.get("storage_mac"):
            raise NetError("BF3 port {} has no netdev (S3)".format(storage.get("port", 1)))
        e = {"match": match("storage_mac", "storage_if"), "dhcp4": False, "dhcp6": False,
             "addresses": [str(host.storage)]}
        if storage.get("mtu"):
            e["mtu"] = int(storage["mtu"])
        if storage.get("routes"):
            e["routes"] = _routes(storage["routes"], storage.get("gateway"))
        eth["mtest-storage"] = e
    ifs = nic.get("ipoib_ifs") or []
    for i, iface in enumerate(host.ib):
        if iface is None:
            continue
        if i >= len(ifs) or not ifs[i]:
            raise NetError("rail {} has no IPoIB interface (S3)".format(i))
        e = {"dhcp4": False, "dhcp6": False, "addresses": [str(iface)]}
        if ipoib.get("mtu"):
            e["mtu"] = int(ipoib["mtu"])
        eth[ifs[i]] = e
    pins = lom_cfg.get("pin_routes") or []
    mgmt_default = bool(mgmt.get("default_route", True) and host.mgmt is not None and mgmt.get("gateway"))
    if pins or (lom_dhcp and mgmt_default):
        lom = nic.get("lom") or {}
        if not lom_id:
            raise NetError("the LOM port {} is not configured by netplan; refusing to change routes".format(
                lom.get("if") or "?"))
        e = {}
        if pins:
            if not lom.get("gateway"):
                raise NetError("no default gateway found on the LOM port {} (S3)".format(lom.get("if")))
            e["routes"] = [{"to": str(n), "via": lom["gateway"]} for n in pins]
        if lom_dhcp and mgmt_default:
            # Keep the LOM's DHCP address but drop its default route (and DNS/NTP when mgmt has DNS).
            overrides = {"use-routes": False}
            if mgmt.get("dns"):
                overrides.update({"use-dns": False, "use-ntp": False})
            e["dhcp4-overrides"] = overrides
        if e:
            eth[lom_id] = e
    doc = {"network": {"version": 2, "ethernets": eth}}
    head = "# Written by mtest (S4). Changes here are overwritten by 'mtest net apply'.\n"
    return head + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


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
    mgmt = network.get("mgmt") or {}
    verify = network.get("verify") or {}
    lom = nic.get("lom") or {}
    args = {"action": "verify", "control_ip": lom.get("control_ip"), "lom_if": lom.get("if"), "ifaces": [],
            "ib": [], "pings": []}
    if mgmt.get("default_route", True) and host.mgmt is not None and mgmt.get("gateway"):
        args["default_if"] = nic.get("mgmt_if")
    if host.mgmt is not None:
        args["ifaces"].append({"label": "mgmt", "name": nic["mgmt_if"], "addr": str(host.mgmt),
                               "mtu": mgmt.get("mtu")})
        if verify.get("ping_gateway", True) and mgmt.get("gateway"):
            args["pings"].append({"label": "mgmt-gateway", "iface": nic["mgmt_if"], "target": str(mgmt["gateway"])})
    if critical_only:
        return args
    storage = network.get("storage") or {}
    if host.storage is not None:
        args["ifaces"].append({"label": "storage", "name": nic["storage_if"], "addr": str(host.storage),
                               "mtu": storage.get("mtu")})
    rate = dig(network, "expect.cx8.rate_gbps")
    ipoib_mtu = dig(network, "ipoib.mtu")
    for i, iface in enumerate(host.ib):
        rail = nic["rails"][i] if i < len(nic.get("rails", [])) else {}
        if iface is not None and rail:
            args["ifaces"].append({"label": "ib{}".format(i), "name": rail["netdev"], "addr": str(iface),
                                   "mtu": ipoib_mtu})
        if rail.get("ibdev"):
            args["ib"].append({"label": "rail{}".format(i), "ibdev": rail["ibdev"], "min_rate": rate})
    if peer is not None and verify.get("ping_peer_in_rack", True):
        ph = peer["host"]
        if host.mgmt is not None and ph.mgmt is not None:
            args["pings"].append({"label": "mgmt-peer", "iface": nic["mgmt_if"], "target": ph.mgmt_ip})
        if host.storage is not None and ph.storage is not None:
            args["pings"].append({"label": "storage-peer", "iface": nic["storage_if"], "target": str(ph.storage.ip)})
        for i, iface in enumerate(host.ib):
            if iface is not None and i < len(ph.ib) and ph.ib[i] is not None and i < len(nic.get("rails", [])):
                args["pings"].append({"label": "ib{}-peer".format(i), "iface": nic["rails"][i]["netdev"],
                                      "target": str(ph.ib[i].ip)})
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
        names = [n for n in (hn.nic.get("ipoib_ifs") or []) + [hn.nic.get("mgmt_if"), hn.nic.get("storage_if")] if n]
        found = conflicts(files, [hn.nic.get("mgmt_mac"), hn.nic.get("storage_mac")], names)
        if found:
            return hn.fail("error", "; ".join(found))
        _, lom_id, lom_entry = find_lom_id(files, hn.nic.get("lom") or {})
        try:
            hn.content = render(hn.host, hn.nic, self.network, lom_id, bool((lom_entry or {}).get("dhcp4")))
        except NetError as e:
            return hn.fail("error", str(e))
        hn.current = info.get("own")
        to_set, pending = link_type_actions(hn.nic, self.network)
        if to_set:
            hn.actions.append("set LINK_TYPE_P1=IB on " + ", ".join(r["pci"] for r in to_set))
        if to_set or pending:
            hn.reboot_needed = True
            hn.actions.append("reboot to apply the IB link type ({})".format(
                ", ".join("rail{}".format(r["rail"]) for r in to_set + pending)))
        hn.to_set = to_set
        if hn.content != hn.current:
            hn.actions.append("write /etc/netplan/{} and netplan apply (rollback after {}s)".format(
                OWN_FILE, self.rollback))

    def diff(self, hn):
        old = (hn.current or "").splitlines(True)
        new = (hn.content or "").splitlines(True)
        return "".join(difflib.unified_diff(old, new, "a/" + OWN_FILE, "b/" + OWN_FILE))

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
        if hn.content == hn.current:
            return
        try:
            res = self.node(hn, {"action": "apply", "content": hn.content, "rollback_seconds": self.rollback})
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
            if not info.get("rollback_active") and info.get("own") == hn.current:
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
                if self.dry_run and hn.content != hn.current:
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
            if hn.content:
                with open(os.path.join(run_dir, hn.host.name, ITEM, OWN_FILE), "w") as f:
                    f.write(hn.content)
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
