import importlib.util
import ipaddress
import os
import tempfile
import unittest

import yaml

from mtest import nic as nicmod
from mtest import net
from mtest.config import Host
from mtest.nodecmd import SCRIPT_DIR


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
    "lom": {"pin_routes": ["10.252.10.0/24"]},
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
      dhcp4: true
"""


class NetplanTest(unittest.TestCase):

    def nic(self):
        rails = [{"rail": i, "netdev": n, "ibdev": "mlx5_" + n[3], "link_type": "IB(1)", "guid": "b83f:d203:00a1:000" + n[3],
                  "link_type_next": "IB(1)", "pci": "000{}:03:00.0".format(i)}
                 for i, n in enumerate(["ibp2s0", "ibp0s0", "ibp3s0", "ibp1s0"])]
        pfs = [{"pci": "0009:01:00.0", "netdev": "enP9p1s0f0np0", "mac": "aa:bb:cc:00:00:01"},
               {"pci": "0009:01:00.1", "netdev": "enP9p1s0f1np1", "mac": "aa:bb:cc:00:00:02"}]
        return {"mgmt_if": "enP9p1s0f0np0", "mgmt_mac": "aa:bb:cc:00:00:01", "storage_if": "enP9p1s0f1np1",
                "storage_mac": "aa:bb:cc:00:00:02", "ipoib_ifs": ["ibp2s0", "ibp0s0", "ibp3s0", "ibp1s0"],
                "rails": rails, "mlxconfig": True, "raw": {"bf3": {"pfs": pfs}, "rails": rails},
                "lom": {"if": "eno1", "mac": "11:22:33:44:55:66", "gateway": "10.10.0.1", "control_ip": "10.252.10.5",
                        "prefix": "10.10.0.11/24"}}

    def test_find_lom_and_conflicts(self):
        files = {"50-cloud-init.yaml": LOM_FILE, "bad.yaml": "network: [\n"}
        self.assertEqual(net.find_lom_id(files, {"if": "eno1", "mac": "11:22:33:44:55:66"}),
                         ("50-cloud-init.yaml", "eno1", yaml.safe_load(LOM_FILE)["network"]["ethernets"]["eno1"]))
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
        text = net.render(host(), self.nic(), NETWORK, "eno1")
        doc = yaml.safe_load(text)["network"]["ethernets"]
        self.assertEqual(doc["mtest-mgmt"]["match"], {"macaddress": "aa:bb:cc:00:00:01"})
        self.assertEqual(doc["mtest-mgmt"]["addresses"], ["10.20.0.11/16"])
        self.assertEqual(doc["mtest-mgmt"]["routes"], [{"to": "default", "via": "10.20.0.1"}])
        self.assertEqual(doc["mtest-mgmt"]["nameservers"], {"addresses": ["10.20.0.53"]})
        self.assertEqual(doc["mtest-storage"]["mtu"], 9000)
        self.assertEqual(doc["mtest-storage"]["routes"], [{"to": "10.30.0.0/16", "scope": "link"}])
        self.assertEqual(doc["ibp0s0"], {"dhcp4": False, "dhcp6": False, "addresses": ["10.101.0.11/16"], "mtu": 4092})
        self.assertEqual(doc["eno1"]["routes"], [{"to": "10.252.10.0/24", "via": "10.10.0.1"}])
        self.assertEqual(doc["eno1"]["dhcp4-overrides"], {"use-routes": False, "use-dns": False, "use-ntp": False})
        self.assertNotIn("match", doc["eno1"])
        self.assertEqual(text, net.render(host(), self.nic(), NETWORK, "eno1"))
        named = yaml.safe_load(net.render(host(), self.nic(), dict(NETWORK, match_by="name"), "eno1"))
        self.assertEqual(named["network"]["ethernets"]["mtest-mgmt"]["match"], {"name": "enP9p1s0f0np0"})
        static = yaml.safe_load(net.render(host(), self.nic(), NETWORK, "eno1", lom_dhcp=False))
        self.assertNotIn("dhcp4-overrides", static["network"]["ethernets"]["eno1"])

    def test_ports_follow_plan_pci(self):
        h = host()
        h.plan.update({"mgmt_pci": "0009:01:00.1", "storage_pci": "0009:01:00.0"})   # swapped on purpose
        doc = yaml.safe_load(net.render(h, self.nic(), NETWORK, "eno1"))["network"]["ethernets"]
        self.assertEqual(doc["mtest-mgmt"]["match"], {"macaddress": "aa:bb:cc:00:00:02"})
        self.assertEqual(doc["mtest-storage"]["match"], {"macaddress": "aa:bb:cc:00:00:01"})

    def test_plan_errors(self):
        cases = [({"mgmt_pci": None}, "mgmt_pci is empty"),
                 ({"storage_pci": "0009:01:00.2"}, "storage_pci 0009:01:00.2 is not a BF3 port"),
                 ({"mgmt_pci": "0000:03:00.0"}, "is not a BF3 port"),
                 ({"mgmt_mac": "aa:bb:cc:00:00:09"}, "differs from the IP plan"),
                 ({"ib_pci": ["0000:03:00.0", "", "0002:03:00.0", "0003:03:00.0"]}, "ib_pci[1] is empty"),
                 ({"ib_pci": ["0009:01:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]},
                  "ib_pci[0] 0009:01:00.0 is not a compute NIC"),
                 ({"ib_guid": ["0xb83fd20300a10009", "", "", ""]}, "GUID")]
        for change, msg in cases:
            h = host()
            h.plan.update(change)
            with self.assertRaises(net.NetError) as cm:
                net.render(h, self.nic(), NETWORK, "eno1")
            self.assertIn(msg, str(cm.exception), change)
        h = host()
        h.plan["ib_guid"] = ["0xB83FD20300A10002", "", "", ""]
        net.render(h, self.nic(), NETWORK, "eno1")

    def test_ib_mtu_only_and_per_host_routes(self):
        h = host(ib=False)
        h.plan.update({"ib_mtu": 2044, "mgmt_gateway": "10.20.0.254", "storage_gateway": "10.30.0.1",
                       "storage_routes": ["10.61.1.0/24"]})
        doc = yaml.safe_load(net.render(h, self.nic(), NETWORK, "eno1"))["network"]["ethernets"]
        self.assertEqual(doc["ibp0s0"], {"dhcp4": False, "dhcp6": False, "link-local": [], "mtu": 2044})
        self.assertEqual(doc["mtest-mgmt"]["routes"], [{"to": "default", "via": "10.20.0.254"}])
        self.assertEqual(doc["mtest-storage"]["routes"], [{"to": "10.61.1.0/24", "via": "10.30.0.1"}])
        args = net.verify_args(h, self.nic(), NETWORK, peer={"host": host("b", tray=2, last=12), "nic": self.nic()})
        ib0 = [i for i in args["ifaces"] if i["label"] == "ib1"][0]
        self.assertEqual((ib0["name"], ib0["addr"], ib0["mtu"]), ("ibp0s0", None, 2044))
        self.assertNotIn("ib0-peer", [p["label"] for p in args["pings"]])
        no_mtu = host(ib=False)
        doc = yaml.safe_load(net.render(no_mtu, self.nic(), dict(NETWORK, ipoib={}), "eno1"))["network"]["ethernets"]
        self.assertNotIn("ibp0s0", doc)

    def test_auto_pin_control_ip(self):
        auto = dict(NETWORK, lom={})
        doc = yaml.safe_load(net.render(host(), self.nic(), auto, "eno1"))["network"]["ethernets"]
        self.assertEqual(doc["eno1"]["routes"], [{"to": "10.252.10.5/32", "via": "10.10.0.1"}])
        nic = self.nic()
        nic["lom"]["control_ip"] = "10.10.0.200"          # same subnet as the LOM: no route needed
        doc = yaml.safe_load(net.render(host(), nic, auto, "eno1"))["network"]["ethernets"]
        self.assertNotIn("routes", doc["eno1"])

    def test_require_pci_false_uses_s3_order(self):
        h = host()
        h.plan = {}
        doc = yaml.safe_load(net.render(h, self.nic(), dict(NETWORK, require_pci=False), "eno1"))
        self.assertEqual(doc["network"]["ethernets"]["mtest-mgmt"]["match"], {"macaddress": "aa:bb:cc:00:00:01"})
        self.assertEqual(doc["network"]["ethernets"]["ibp2s0"]["addresses"], ["10.100.0.11/16"])

    def test_render_refuses_unsafe(self):
        with self.assertRaises(net.NetError):
            net.render(host(), self.nic(), NETWORK, None)
        nic = self.nic()
        nic["lom"]["gateway"] = ""
        with self.assertRaises(net.NetError):
            net.render(host(), nic, NETWORK, "eno1")
        nic = self.nic()
        nic["raw"]["bf3"]["pfs"][0]["mac"] = ""
        with self.assertRaises(net.NetError):
            net.render(host(), nic, NETWORK, "eno1")

    def test_lom_default_kept_without_mgmt_default(self):
        for network in (dict(NETWORK, mgmt=dict(NETWORK["mgmt"], default_route=False)),
                        dict(NETWORK, mgmt=dict(NETWORK["mgmt"], gateway=None))):
            doc = yaml.safe_load(net.render(host(), self.nic(), network, "eno1"))["network"]["ethernets"]
            self.assertNotIn("dhcp4-overrides", doc["eno1"])
            self.assertEqual(doc["eno1"]["routes"], [{"to": "10.252.10.0/24", "via": "10.10.0.1"}])
            self.assertNotIn("default_if", net.verify_args(host(), self.nic(), network))
        no_mgmt = host()
        no_mgmt.mgmt = None
        doc = yaml.safe_load(net.render(no_mgmt, self.nic(), NETWORK, "eno1"))["network"]["ethernets"]
        self.assertNotIn("dhcp4-overrides", doc["eno1"])
        # Nothing to change on the LOM: no entry, and an unmanaged LOM is then acceptable.
        quiet = dict(NETWORK, lom={}, mgmt=dict(NETWORK["mgmt"], default_route=False))
        doc = yaml.safe_load(net.render(host(), self.nic(), quiet, None))["network"]["ethernets"]
        self.assertNotIn("eno1", doc)

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
    """The restore and confirm halves of net_node.py, run against temporary directories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.mod = load_node("net_node.py")
        self.mod.NETPLAN, self.mod.BACKUP, self.mod.CLAIM = t + "/netplan", t + "/backup", t + "/claim"
        self.mod.OWN = t + "/netplan/60-mtest.yaml"
        w(t + "/bin/netplan", "#!/bin/sh\nexit 0\n")
        os.chmod(t + "/bin/netplan", 0o755)
        self.env = dict(os.environ, PATH=t + "/bin:" + os.environ["PATH"])
        w(t + "/netplan/50-lom.yaml", "new-lom")
        w(t + "/netplan/60-mtest.yaml", "new-mtest")
        w(t + "/backup/50-lom.yaml", "old-lom")

    def tearDown(self):
        self.tmp.cleanup()

    def restore(self):
        import subprocess
        return subprocess.run(["/bin/sh", "-c", self.mod.restore_cmd()], env=self.env).returncode

    def files(self):
        d = self.mod.NETPLAN
        out = {}
        for f in sorted(os.listdir(d)):
            with open(os.path.join(d, f)) as fh:
                out[f] = fh.read()
        return out

    def test_restore_copies_back_and_removes_new_files(self):
        self.assertEqual(self.restore(), 0)
        self.assertEqual(self.files(), {"50-lom.yaml": "old-lom"})
        w(self.mod.NETPLAN + "/60-mtest.yaml", "again")
        self.assertEqual(self.restore(), 0)          # claim already taken: does nothing
        self.assertIn("60-mtest.yaml", self.files())

    def test_restore_without_backup_changes_nothing(self):
        import shutil
        shutil.rmtree(self.mod.BACKUP)
        self.assertEqual(self.restore(), 1)
        self.assertEqual(self.files(), {"50-lom.yaml": "new-lom", "60-mtest.yaml": "new-mtest"})

    def test_confirm_and_rollback_exclude_each_other(self):
        self.mod.run = lambda *a, **k: (0, "")
        self.assertEqual(self.mod.confirm({})["ok"], True)
        self.assertFalse(os.path.exists(self.mod.BACKUP))
        self.assertEqual(self.restore(), 0)
        self.assertIn("60-mtest.yaml", self.files())  # rollback backed off
        os.rmdir(self.mod.CLAIM)
        w(self.mod.BACKUP + "/50-lom.yaml", "old-lom")
        self.assertEqual(self.restore(), 0)
        self.assertEqual(self.mod.confirm({}), {"ok": False, "rolled_back": True})


if __name__ == "__main__":
    unittest.main()
