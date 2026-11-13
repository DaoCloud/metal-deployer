import importlib.util
import ipaddress
import os
import tempfile
import unittest

import yaml

from testkit import nic as nicmod
from testkit import net
from testkit.config import Host
from testkit.util import deep_merge
from testkit.nodecmd import SCRIPT_DIR


def load_node(name):
    spec = importlib.util.spec_from_file_location(name[:-3], os.path.join(SCRIPT_DIR, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def fake_sysfs(root, swap_gpu=False):
    """A GB300-like tree: 4 GPUs, each behind the same switch as one CX8; BF3 with 2 PFs."""
    dev = os.path.join(root, "devices")
    bus = os.path.join(root, "bus/pci/devices")
    os.makedirs(bus)

    def pci(addr, parent, vendor, device, numa, netdev=None, ibdev=None, mac="", state="4: ACTIVE",
            rate="800 Gb/sec (4X XDR)", ll="InfiniBand"):
        path = os.path.join(dev, parent, addr)
        w(os.path.join(path, "vendor"), vendor)
        w(os.path.join(path, "device"), device)
        w(os.path.join(path, "numa_node"), str(numa))
        os.symlink(path, os.path.join(bus, addr))
        if netdev:
            os.makedirs(os.path.join(path, "net", netdev))
            w(os.path.join(root, "class/net", netdev, "address"), mac)
        if ibdev:
            os.makedirs(os.path.join(path, "infiniband", ibdev))
            base = os.path.join(root, "class/infiniband", ibdev)
            w(os.path.join(base, "ports/1/state"), state)
            w(os.path.join(base, "ports/1/rate"), rate)
            w(os.path.join(base, "ports/1/link_layer"), ll)
            w(os.path.join(base, "node_guid"), "0000:0000:0000:000" + ibdev[-1])

    # CX8 PCI addresses deliberately not in GPU order: rail mapping must follow the PCI tree.
    order = [2, 0, 3, 1]
    for g in range(4):
        sw = "pci000{}:00/000{}:00:00.0/000{}:01:00.0".format(g, g, g)
        pci("000{}:02:00.0".format(g), sw, "0x10de", "0x2901", g // 2)                      # GPU g
        n = order[g]
        pci("000{}:03:00.0".format(g), sw, "0x15b3", "0x1023", g // 2, netdev="ibp{}s0".format(n),
            ibdev="mlx5_{}".format(n), mac="00:00:10:{}".format(n), state="1: DOWN" if n == 3 else "4: ACTIVE")
    pci("0009:01:00.0", "pci0009:00", "0x15b3", "0xa2dc", 0, netdev="enP9p1s0f0np0", ibdev="mlx5_4",
        mac="aa:bb:cc:00:00:01", ll="Ethernet", rate="400 Gb/sec")
    pci("0009:01:00.1", "pci0009:00", "0x15b3", "0xa2dc", 0, netdev="enP9p1s0f1np1", ibdev="mlx5_5",
        mac="aa:bb:cc:00:00:02", ll="Ethernet", rate="400 Gb/sec")
    pci("0009:01:00.2", "pci0009:00", "0x15b3", "0xc2d5", 0)                                   # BF3 SoC mgmt
    pci("0005:01:00.0", "pci0005:00", "0x8086", "0x1533", 0, netdev="eno1", mac="11:22:33:44:55:66")
    gpus = ["0000000{}:02:00.0".format(g) for g in range(4)]
    if swap_gpu:
        gpus[0], gpus[1] = gpus[1], gpus[0]
    return gpus


NETWORK = {
    "expect": {"gpus": 4, "cx8": {"count": 4, "pci_id": "15b3:1023", "link_layer": "InfiniBand", "rate_gbps": 800},
               "bf3": {"pf_count": 2, "pci_id": "15b3:a2dc", "mode": "nic"}},
    "lom": {},
    "mgmt": {"port": 0, "gateway": "10.20.0.1", "mtu": 1500, "dns": ["10.20.0.53"], "default_route": True},
    "storage": {"port": 1, "mtu": 9000, "routes": ["10.30.0.0/16"]},
    "ipoib": {"mtu": 4092},
}


def host(name="gb300-r1-t01", rack="R1", tray=1, last=11, ib=True):
    h = Host(name, rack, tray, "10.10.0.{}".format(last),
             mgmt=ipaddress.ip_interface("10.20.0.{}/16".format(last)),
             storage=ipaddress.ip_interface("10.30.0.{}/16".format(last)),
             ib=[ipaddress.ip_interface("10.10{}.0.{}/16".format(k, last)) for k in range(4)] if ib else [])
    h.plan = {"mgmt_pci": "0009:01:00.0", "storage_pci": "0009:01:00.1",
              "ib_pci": ["000{}:03:00.0".format(i) for i in range(4)]}
    return h


class DiscoverTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.mod = load_node("nic_discover.py")

    def tearDown(self):
        self.tmp.cleanup()

    def discover(self, gpus):
        raw = self.mod.discover({"cx8_ids": ["1023"], "bf3_ids": ["a2dc"], "mlxconfig": False,
                                 "sysfs": self.root, "fake_gpus": gpus})
        raw["lom"] = {"if": "eno1", "ip": "10.10.0.11", "mac": "11:22:33:44:55:66", "gateway": "10.10.0.1",
                      "control_ip": "10.252.10.5"}
        return raw

    def test_rails_follow_pci_tree(self):
        raw = self.discover(fake_sysfs(self.root))
        self.assertEqual([r["ibdev"] for r in raw["rails"]], ["mlx5_2", "mlx5_0", "mlx5_3", "mlx5_1"])
        self.assertEqual([r["gpu"] for r in raw["rails"]], [0, 1, 2, 3])
        self.assertEqual([r["numa"] for r in raw["rails"]], [0, 0, 1, 1])
        self.assertEqual(raw["rails"][2]["state"], "DOWN")
        self.assertEqual(raw["rails"][0]["rate_gbps"], 800.0)
        self.assertEqual([p["netdev"] for p in raw["bf3"]["pfs"]], ["enP9p1s0f0np0", "enP9p1s0f1np1"])

    def test_without_gpus_uses_pci_order(self):
        fake_sysfs(self.root)
        raw = self.discover([])
        self.assertEqual([r["pci"] for r in raw["rails"]], ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0",
                                                            "0003:03:00.0"])
        self.assertEqual({r["gpu"] for r in raw["rails"]}, {None})

    def test_summary_checks_and_golden(self):
        gpus = fake_sysfs(self.root)
        rec = nicmod.summarize(self.discover(gpus), NETWORK)
        self.assertEqual(rec["mgmt_if"], "enP9p1s0f0np0")
        self.assertEqual(rec["storage_mac"], "aa:bb:cc:00:00:02")
        self.assertEqual(rec["ib_devs"], ["mlx5_2", "mlx5_0", "mlx5_3", "mlx5_1"])
        checks = nicmod.check(rec, NETWORK, golden=rec)
        self.assertEqual(checks["cx8_port_active"]["result"], "fail")
        self.assertIn("rail2 DOWN", checks["cx8_port_active"]["reason"])
        self.assertEqual(checks["bf3_mode"]["result"], "skip")
        for name in ("gpus", "cx8_count", "cx8_link_layer", "bf3_ports", "lom", "golden_match", "gpu_rail_map"):
            self.assertEqual(checks[name]["result"], "pass", name)
        other = nicmod.summarize(self.discover(fake_sysfs(os.path.join(self.root, "b"), swap_gpu=True)), NETWORK)
        diff = nicmod.check(other, NETWORK, golden=rec)["golden_match"]
        self.assertEqual(diff["result"], "fail")
        self.assertIn("rail0", diff["reason"])

    def test_apply_plan(self):
        rec = nicmod.summarize(self.discover(fake_sysfs(self.root)), NETWORK)
        h = host()
        h.plan = {"mgmt_pci": "0009:01:00.1", "storage_pci": "0009:01:00.0",
                  "ib_pci": ["0002:03:00.0", "0000:03:00.0", "0003:03:00.0", "0001:03:00.0"]}
        self.assertEqual(nicmod.apply_plan(rec, h)["result"], "fail")      # rails are in GPU order, not this one
        self.assertEqual(rec["mgmt_if"], "enP9p1s0f1np1")
        h.plan["ib_pci"] = ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]
        self.assertEqual(nicmod.apply_plan(rec, h), {"result": "pass", "reason": "all PCI addresses found"})
        h.plan["mgmt_pci"] = "0009:01:00.5"
        self.assertIn("mgmt_pci 0009:01:00.5 not found", nicmod.apply_plan(rec, h)["reason"])
        h.plan = {}
        self.assertIsNone(nicmod.apply_plan(rec, h))

    def test_plain_nic_and_lom_roles(self):
        fake_sysfs(self.root)
        raw = self.mod.discover({"cx8_ids": ["1023"], "bf3_ids": ["a2dc"], "mlxconfig": False, "sysfs": self.root,
                                 "fake_gpus": [], "nic_pcis": ["0005:01:00.0", "0006:01:00.0"]})
        self.assertEqual(raw["nics"], [{"pci": "0005:01:00.0", "vendor": "8086", "device": "1533", "netdev": "eno1",
                                        "mac": "11:22:33:44:55:66"}])
        raw["lom"] = {"if": "eno2", "mac": "11:22:33:44:55:77"}
        custom = deep_merge(NETWORK, {"mgmt": {"device": "nic"}, "storage": {"device": "nic"}})
        rec = nicmod.summarize(raw, custom)
        self.assertEqual((rec["mgmt_if"], rec["storage_if"]), ("", ""))   # not BF3 ports any more
        h = host()
        h.plan = {"mgmt_pci": "0005:01:00.0"}
        check = nicmod.apply_plan(rec, h, custom)
        self.assertEqual((rec["mgmt_if"], rec["mgmt_mac"]), ("eno1", "11:22:33:44:55:66"))
        self.assertEqual(check["reason"], "storage_pci is empty (network.storage.device is nic)")
        on_lom = deep_merge(NETWORK, {"mgmt": {"device": "lom"}})
        self.assertEqual(nicmod.summarize(raw, on_lom)["mgmt_if"], "eno2")

    def test_bf3_mode(self):
        rec = nicmod.summarize(self.discover(fake_sysfs(self.root)), NETWORK)
        rec["bf3"].update({"mode": "DISABLED(1)", "mode_next": "DISABLED(1)"})
        self.assertEqual(nicmod.check(rec, NETWORK)["bf3_mode"]["result"], "pass")
        rec["bf3"].update({"mode": "ENABLED(0)", "mode_next": "DISABLED(1)"})
        c = nicmod.check(rec, NETWORK)["bf3_mode"]
        self.assertEqual(c["result"], "fail")
        self.assertIn("pending", c["reason"])


LOM_FILE = """network:
  version: 2
  ethernets:
    eno1:
      match: {macaddress: "11:22:33:44:55:66"}
      set-name: eno1
      addresses: [10.10.0.11/24]
      routes:
        - {to: default, via: 10.10.0.1}
        - {to: 10.252.0.0/16, via: 10.10.0.1}
      nameservers: {addresses: [10.10.0.53]}
"""
LOM_ENTRY = yaml.safe_load(LOM_FILE)["network"]["ethernets"]["eno1"]
HOST = {"50-cloud-init.yaml": LOM_FILE}
NO_LOM = {}
MS, COMPUTE = "90-static-mgmt-storage.yaml", "90-static-compute.yaml"


def lom_file(**entry):
    """A static LOM netplan file whose eno1 entry is entry."""
    return {"50-cloud-init.yaml": yaml.safe_dump({"network": {"version": 2, "ethernets": {"eno1": entry}}})}


def eths(files):
    """All ethernets entries of rendered files, keyed by file then id."""
    return {name: yaml.safe_load(text)["network"]["ethernets"] for name, text in files.items()}


class NetplanTest(unittest.TestCase):

    def nic(self):
        rails = [{"rail": i, "netdev": n, "ibdev": "mlx5_" + n[3], "link_type": "IB(1)", "guid": "b83f:d203:00a1:000" + n[3],
                  "link_type_next": "IB(1)", "pci": "000{}:03:00.0".format(i)}
                 for i, n in enumerate(["ibp2s0", "ibp0s0", "ibp3s0", "ibp1s0"])]
        pfs = [{"pci": "0009:01:00.0", "netdev": "enP9p1s0f0np0", "mac": "aa:bb:cc:00:00:01"},
               {"pci": "0009:01:00.1", "netdev": "enP9p1s0f1np1", "mac": "aa:bb:cc:00:00:02"}]
        nics = [{"pci": "0005:02:00.0", "netdev": "enp5s0f0", "mac": "dd:ee:ff:00:00:01"},
                {"pci": "0005:02:00.1", "netdev": "enp5s0f1", "mac": "dd:ee:ff:00:00:02"}]
        return {"mgmt_if": "enP9p1s0f0np0", "mgmt_mac": "aa:bb:cc:00:00:01", "storage_if": "enP9p1s0f1np1",
                "storage_mac": "aa:bb:cc:00:00:02", "ipoib_ifs": ["ibp2s0", "ibp0s0", "ibp3s0", "ibp1s0"],
                "rails": rails, "mlxconfig": True, "raw": {"bf3": {"pfs": pfs}, "rails": rails, "nics": nics},
                "lom": {"if": "eno1", "mac": "11:22:33:44:55:66", "gateway": "10.10.0.1", "control_ip": "10.252.10.5",
                        "ip": "10.10.0.11", "prefix": "10.10.0.11/24"}}

    def render(self, h=None, nic=None, network=NETWORK, files=HOST):
        own, _ = net.render(h or host(), nic or self.nic(), network, files)
        return eths(own)

    def test_find_lom_and_conflicts(self):
        files = {"50-cloud-init.yaml": LOM_FILE, "bad.yaml": "network: [\n"}
        self.assertEqual(net.find_lom_id(files, {"if": "eno1", "mac": "11:22:33:44:55:66"}),
                         ("50-cloud-init.yaml", "eno1", LOM_ENTRY))
        self.assertEqual(net.find_lom_id({"a.yaml": "network: {ethernets: {lom: {match: {name: 'en*'}}}}"},
                                         {"if": "eno1"})[:2], ("a.yaml", "lom"))
        self.assertEqual(net.find_lom_id({"a.yaml": "network: {ethernets: {eno1: {dhcp4: true}}}"},
                                         {"if": "eno1"})[:2], ("a.yaml", "eno1"))
        self.assertEqual(net.find_lom_id({}, {"if": "eno1"}), (None, None, None))
        found = net.conflicts({"50.yaml": "network: {ethernets: {all: {match: {name: 'ib*'}, dhcp4: true}, "
                                          "x: {match: {macaddress: 'AA:BB:CC:00:00:01'}}}}"},
                              ["aa:bb:cc:00:00:01"], ["ibp0s0"])
        self.assertEqual(len(found), 2)
        self.assertEqual(net.conflicts({"50.yaml": LOM_FILE}, ["aa:bb:cc:00:00:01"], ["ibp0s0"]), [])

    def test_render(self):
        own, edits = net.render(host(), self.nic(), NETWORK, HOST)
        self.assertEqual(sorted(own), [COMPUTE, MS])
        doc = eths(own)
        mgmt = doc[MS]["testkit-mgmt"]
        self.assertEqual(mgmt["match"], {"macaddress": "aa:bb:cc:00:00:01"})
        self.assertEqual(mgmt["addresses"], ["10.20.0.11/16"])
        self.assertEqual(mgmt["routes"], [{"to": "default", "via": "10.20.0.1"}])
        self.assertEqual(mgmt["nameservers"], {"addresses": ["10.20.0.53"]})
        storage = doc[MS]["testkit-storage"]
        self.assertEqual(storage["mtu"], 9000)
        self.assertEqual(storage["routes"], [{"to": "10.30.0.0/16", "scope": "link"}])
        self.assertEqual(sorted(doc[COMPUTE]), ["ibp0s0", "ibp1s0", "ibp2s0", "ibp3s0"])
        self.assertEqual(doc[COMPUTE]["ibp0s0"],
                         {"dhcp4": False, "dhcp6": False, "addresses": ["10.101.0.11/16"], "mtu": 4092})
        # Policy routing merged into the LOM id; the LOM default route itself moves into table 100.
        self.assertEqual(doc[MS]["eno1"], {
            "routes": [{"to": "10.10.0.0/24", "scope": "link", "table": 100}],
            "routing-policy": [{"from": "10.10.0.11/32", "table": 100, "priority": 100}]})
        self.assertEqual(list(edits), ["50-cloud-init.yaml"])
        lom = yaml.safe_load(edits["50-cloud-init.yaml"])["network"]["ethernets"]["eno1"]
        self.assertEqual(lom["routes"], [{"to": "default", "via": "10.10.0.1", "table": 100},
                                         {"to": "10.252.0.0/16", "via": "10.10.0.1"}])
        self.assertEqual(dict(lom, routes=None), dict(LOM_ENTRY, routes=None))   # the rest is untouched
        self.assertEqual((own, edits), net.render(host(), self.nic(), NETWORK, HOST))
        # Second run: the default route is already in table 100, nothing more to change there.
        own2, edits2 = net.render(host(), self.nic(), NETWORK, edits)
        self.assertEqual((own2, edits2), (own, {}))
        named = self.render(network=dict(NETWORK, match_by="name"))
        self.assertEqual(named[MS]["testkit-mgmt"]["match"], {"name": "enP9p1s0f0np0"})

    def test_lom_policy(self):
        lom = self.render(network=dict(NETWORK, lom={"table": 200, "priority": 50}))[MS]["eno1"]
        self.assertEqual(lom["routing-policy"], [{"from": "10.10.0.11/32", "table": 200, "priority": 50}])
        own, edits = net.render(host(), self.nic(), NETWORK,
                                lom_file(addresses=["10.10.0.11/24"], gateway4="10.10.0.254"))
        lom = yaml.safe_load(edits["50-cloud-init.yaml"])["network"]["ethernets"]["eno1"]
        self.assertNotIn("gateway4", lom)
        self.assertEqual(lom["routes"], [{"to": "default", "via": "10.10.0.254", "table": 100}])
        # No default route on the LOM: testkit adds the table default via the LOM gateway itself.
        own, edits = net.render(host(), self.nic(), NETWORK, lom_file(addresses=["10.10.0.11/24"]))
        self.assertEqual(edits, {})
        self.assertEqual(eths(own)[MS]["eno1"]["routes"], [{"to": "10.10.0.0/24", "scope": "link", "table": 100},
                                                           {"to": "default", "via": "10.10.0.1", "table": 100}])
        cases = [(lom_file(dhcp4=True), "uses DHCP"),
                 (lom_file(addresses=["10.10.0.99/24"]), "does not configure the address")]
        for files, msg in cases:
            with self.assertRaises(net.NetError) as cm:
                net.render(host(), self.nic(), NETWORK, files)
            self.assertIn(msg, str(cm.exception))
        h = host()
        h.plan["lom_gateway"] = "10.99.0.1"
        with self.assertRaises(net.NetError) as cm:
            net.render(h, self.nic(), NETWORK, lom_file(addresses=["10.10.0.11/24"]))
        self.assertIn("not in the LOM subnet", str(cm.exception))

    def test_ports_follow_plan_pci(self):
        h = host()
        h.plan.update({"mgmt_pci": "0009:01:00.1", "storage_pci": "0009:01:00.0"})   # swapped on purpose
        doc = self.render(h)
        self.assertEqual(doc[MS]["testkit-mgmt"]["match"], {"macaddress": "aa:bb:cc:00:00:02"})
        self.assertEqual(doc[MS]["testkit-storage"]["match"], {"macaddress": "aa:bb:cc:00:00:01"})

    def test_plain_nic_roles(self):
        custom = deep_merge(NETWORK, {"mgmt": {"device": "nic"}, "storage": {"device": "nic"},
                                      "require_pci": False})
        h = host()
        h.plan.update({"mgmt_pci": "0005:02:00.0", "storage_pci": "0005:02:00.1"})
        doc = self.render(h, network=custom)
        self.assertEqual(doc[MS]["testkit-mgmt"]["match"], {"macaddress": "dd:ee:ff:00:00:01"})
        self.assertEqual(doc[MS]["testkit-storage"]["match"], {"macaddress": "dd:ee:ff:00:00:02"})
        h.plan["mgmt_pci"] = "0009:01:00.0"                       # any network port will do, BF3 included
        self.assertEqual(self.render(h, network=custom)[MS]["testkit-mgmt"]["match"],
                         {"macaddress": "aa:bb:cc:00:00:01"})
        for change, msg in (({"mgmt_pci": None}, "mgmt_pci is empty in the IP plan (network.mgmt.device is nic)"),
                            ({"storage_pci": "0000:03:00.0"}, "it is a compute rail"),
                            ({"storage_pci": "0007:00:00.0"}, "not a network port found on this host")):
            h2 = host()
            h2.plan.update({"mgmt_pci": "0005:02:00.0", "storage_pci": "0005:02:00.1"})
            h2.plan.update(change)
            with self.assertRaises(net.NetError) as cm:
                net.render(h2, self.nic(), custom, HOST)
            self.assertIn(msg, str(cm.exception))

    def test_mgmt_on_lom(self):
        on_lom = deep_merge(NETWORK, {"mgmt": {"device": "lom"}})
        h = host()
        h.plan.pop("mgmt_pci")
        own, edits = net.render(h, self.nic(), on_lom, NO_LOM)
        self.assertEqual((sorted(eths(own)[MS]), edits), (["testkit-storage"], {}))
        args = net.verify_args(h, self.nic(), on_lom)
        self.assertNotIn("default_if", args)
        self.assertNotIn("control_src", args)
        self.assertNotIn("mgmt", [i["label"] for i in args["ifaces"]])

    def test_plan_errors(self):
        cases = [({"mgmt_pci": None}, "mgmt_pci is empty"),
                 ({"storage_pci": "0009:01:00.2"}, "storage_pci 0009:01:00.2 is not a BF3 port"),
                 ({"mgmt_pci": "0000:03:00.0"}, "is not a BF3 port"),
                 ({"mgmt_pci": "0005:02:00.0"}, "is not a BF3 port"),
                 ({"mgmt_mac": "aa:bb:cc:00:00:09"}, "differs from the IP plan"),
                 ({"ib_pci": ["0000:03:00.0", "", "0002:03:00.0", "0003:03:00.0"]}, "ib_pci[1] is empty"),
                 ({"ib_pci": ["0009:01:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]},
                  "ib_pci[0] 0009:01:00.0 is not a compute NIC"),
                 ({"ib_guid": ["0xb83fd20300a10009", "", "", ""]}, "GUID")]
        for change, msg in cases:
            h = host()
            h.plan.update(change)
            with self.assertRaises(net.NetError) as cm:
                net.render(h, self.nic(), NETWORK, HOST)
            self.assertIn(msg, str(cm.exception), change)
        h = host()
        h.plan["ib_guid"] = ["0xB83FD20300A10002", "", "", ""]
        net.render(h, self.nic(), NETWORK, HOST)

    def test_ib_mtu_only_and_per_host_routes(self):
        h = host(ib=False)
        h.plan.update({"ib_mtu": 2044, "mgmt_gateway": "10.20.0.254", "storage_gateway": "10.30.0.1",
                       "storage_routes": ["10.61.1.0/24"]})
        doc = self.render(h)
        self.assertEqual(doc[COMPUTE]["ibp0s0"], {"dhcp4": False, "dhcp6": False, "link-local": [], "mtu": 2044})
        self.assertEqual(doc[MS]["testkit-mgmt"]["routes"], [{"to": "default", "via": "10.20.0.254"}])
        self.assertEqual(doc[MS]["testkit-storage"]["routes"],
                         [{"to": "10.61.1.0/24", "via": "10.30.0.1"}])
        args = net.verify_args(h, self.nic(), NETWORK, peer={"host": host("b", tray=2, last=12), "nic": self.nic()})
        ib0 = [i for i in args["ifaces"] if i["label"] == "ib1"][0]
        self.assertEqual((ib0["name"], ib0["addr"], ib0["mtu"]), ("ibp0s0", None, 2044))
        self.assertNotIn("ib0-peer", [p["label"] for p in args["pings"]])
        no_mtu = host(ib=False)
        self.assertNotIn(COMPUTE, net.render(no_mtu, self.nic(), dict(NETWORK, ipoib={}), HOST)[0])

    def test_require_pci_false_uses_s3_order(self):
        h = host()
        h.plan = {}
        doc = self.render(h, network=dict(NETWORK, require_pci=False))
        self.assertEqual(doc[MS]["testkit-mgmt"]["match"], {"macaddress": "aa:bb:cc:00:00:01"})
        self.assertEqual(doc[COMPUTE]["ibp2s0"]["addresses"], ["10.100.0.11/16"])

    def test_render_refuses_unsafe(self):
        with self.assertRaises(net.NetError):
            net.render(host(), self.nic(), NETWORK, NO_LOM)
        nic = self.nic()
        nic["lom"]["gateway"] = ""
        with self.assertRaises(net.NetError):          # gateway needed for the table default route
            net.render(host(), nic, NETWORK, lom_file(addresses=["10.10.0.11/24"]))
        nic = self.nic()
        nic["lom"]["prefix"] = ""
        with self.assertRaises(net.NetError):
            net.render(host(), nic, NETWORK, HOST)
        nic = self.nic()
        nic["raw"]["bf3"]["pfs"][0]["mac"] = ""
        with self.assertRaises(net.NetError):
            net.render(host(), nic, NETWORK, HOST)

    def test_lom_untouched_without_mgmt_default(self):
        for network in (dict(NETWORK, mgmt=dict(NETWORK["mgmt"], default_route=False)),
                        dict(NETWORK, mgmt=dict(NETWORK["mgmt"], gateway=None))):
            # Nothing to change on the LOM, and an unmanaged LOM is then acceptable.
            own, edits = net.render(host(), self.nic(), network, NO_LOM)
            self.assertEqual((sorted(eths(own)[MS]), edits), (["testkit-mgmt", "testkit-storage"], {}))
            self.assertNotIn("default_if", net.verify_args(host(), self.nic(), network))
        no_mgmt = host()
        no_mgmt.mgmt = None
        self.assertEqual(sorted(eths(net.render(no_mgmt, self.nic(), NETWORK, NO_LOM)[0])[MS]), ["testkit-storage"])

    def test_link_type_actions(self):
        nic = self.nic()
        nic["rails"][1]["link_type"] = "ETH(2)"
        nic["rails"][1]["link_type_next"] = "ETH(2)"
        nic["rails"][2]["link_type"] = "ETH(2)"
        to_set, pending = net.link_type_actions(nic, NETWORK)
        self.assertEqual([r["rail"] for r in to_set], [1])
        self.assertEqual([r["rail"] for r in pending], [2])
        self.assertEqual(net.link_type_actions(nic, {"ib_mode": {"enforce": False}}), ([], []))

    def test_verify_args_and_peers(self):
        a, b, c = host(), host("gb300-r1-t02", tray=2, last=12), host("gb300-r2-t01", rack="R2", last=21)
        p = net.peers([a, b, c])
        self.assertEqual((p[a.name].name, p[b.name].name, p[c.name].name), (b.name, a.name, a.name))
        args = net.verify_args(a, self.nic(), NETWORK, peer={"host": b, "nic": self.nic()})
        self.assertEqual(args["default_if"], "enP9p1s0f0np0")
        self.assertEqual(args["control_src"], "10.10.0.11")
        self.assertEqual([i["label"] for i in args["ifaces"]], ["mgmt", "storage", "ib0", "ib1", "ib2", "ib3"])
        self.assertEqual(args["ib"][0], {"label": "rail0", "ibdev": "mlx5_2", "min_rate": 800})
        pings = {x["label"]: x["target"] for x in args["pings"]}
        self.assertEqual(pings, {"mgmt-gateway": "10.20.0.1", "mgmt-peer": "10.20.0.12", "storage-peer": "10.30.0.12",
                                 "ib0-peer": "10.100.0.12", "ib1-peer": "10.101.0.12", "ib2-peer": "10.102.0.12",
                                 "ib3-peer": "10.103.0.12"})
        crit = net.verify_args(a, self.nic(), NETWORK, critical_only=True)
        self.assertEqual([i["label"] for i in crit["ifaces"]], ["mgmt"])
        self.assertEqual(net.verify_args(a, self.nic(), NETWORK, peer=None)["pings"][0]["label"], "mgmt-gateway")


class RollbackScriptTest(unittest.TestCase):
    """The apply, restore and confirm parts of net_node.py, run against temporary directories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.mod = load_node("net_node.py")
        self.mod.NETPLAN, self.mod.BACKUP, self.mod.CLAIM = t + "/netplan", t + "/backup", t + "/claim"
        self.mod.STATE = t
        w(t + "/bin/netplan", "#!/bin/sh\nexit 0\n")
        os.chmod(t + "/bin/netplan", 0o755)
        self.env = dict(os.environ, PATH=t + "/bin:" + os.environ["PATH"])
        w(t + "/netplan/50-lom.yaml", "new-lom")
        w(t + "/netplan/90-static-compute.yaml", "new-testkit")
        w(t + "/backup/50-lom.yaml", "old-lom")

    def tearDown(self):
        self.tmp.cleanup()

    def restore(self):
        import subprocess
        return subprocess.run(["/bin/sh", "-c", self.mod.restore_cmd()], env=self.env).returncode

    def files(self, d=None):
        d = d or self.mod.NETPLAN
        out = {}
        for f in sorted(os.listdir(d)):
            with open(os.path.join(d, f)) as fh:
                out[f] = fh.read()
        return out

    def test_apply_writes_own_files_and_edits(self):
        from unittest import mock
        self.mod.run = lambda cmd, **k: (1, "") if "is-active" in cmd else (0, "")
        apply = lambda files, edits: self.mod.apply({"files": files, "edits": edits, "rollback_seconds": 60})
        with mock.patch.object(self.mod.shutil, "which", return_value="/bin/true"), \
                mock.patch.object(self.mod.subprocess, "Popen"):
            self.assertIn("changed since testkit read it",
                          apply({}, {"50-lom.yaml": {"old": "old-lom", "new": "x"}})["error"])
            self.assertIn("unexpected file name", apply({"../50-lom.yaml": "x"}, {})["error"])
            self.assertIn("unexpected file name", apply({}, {"90-static-compute.yaml": {"old": "", "new": ""}})["error"])
            self.assertEqual(self.files(), {"50-lom.yaml": "new-lom", "90-static-compute.yaml": "new-testkit"})
            res = apply({"90-static-mgmt-storage.yaml": "ms"}, {"50-lom.yaml": {"old": "new-lom", "new": "moved"}})
            self.assertEqual(res, {"ok": True})
            self.assertEqual(self.files(), {"50-lom.yaml": "moved", "90-static-mgmt-storage.yaml": "ms"})
            self.assertEqual(self.files(self.mod.BACKUP), {"50-lom.yaml": "new-lom",
                                                           "90-static-compute.yaml": "new-testkit"})
        info = self.mod.inspect({})
        self.assertEqual((list(info["own"]), list(info["files"])), (["90-static-mgmt-storage.yaml"], ["50-lom.yaml"]))

    def test_restore_copies_back_and_removes_new_files(self):
        self.assertEqual(self.restore(), 0)
        self.assertEqual(self.files(), {"50-lom.yaml": "old-lom"})
        w(self.mod.NETPLAN + "/90-static-compute.yaml", "again")
        self.assertEqual(self.restore(), 0)          # claim already taken: does nothing
        self.assertIn("90-static-compute.yaml", self.files())

    def test_restore_without_backup_changes_nothing(self):
        import shutil
        shutil.rmtree(self.mod.BACKUP)
        self.assertEqual(self.restore(), 1)
        self.assertEqual(self.files(), {"50-lom.yaml": "new-lom", "90-static-compute.yaml": "new-testkit"})

    def test_confirm_and_rollback_exclude_each_other(self):
        self.mod.run = lambda *a, **k: (0, "")
        self.assertEqual(self.mod.confirm({})["ok"], True)
        self.assertFalse(os.path.exists(self.mod.BACKUP))
        self.assertEqual(self.restore(), 0)
        self.assertIn("90-static-compute.yaml", self.files())  # rollback backed off
        os.rmdir(self.mod.CLAIM)
        w(self.mod.BACKUP + "/50-lom.yaml", "old-lom")
        self.assertEqual(self.restore(), 0)
        self.assertEqual(self.mod.confirm({}), {"ok": False, "rolled_back": True})


if __name__ == "__main__":
    unittest.main()
