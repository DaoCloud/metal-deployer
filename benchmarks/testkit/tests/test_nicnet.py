import importlib.util
import ipaddress
import os
import tempfile
import unittest

from testkit import nic as nicmod
from testkit.config import Host
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
}


def host(name="gb300-r1-t01", rack="R1", tray=1, last=11, ib=True):
    h = Host(name, rack, tray, "10.10.0.{}".format(last),
             mgmt=ipaddress.ip_interface("10.20.0.{}/16".format(last)),
             storage=ipaddress.ip_interface("10.30.0.{}/16".format(last)),
             ib=[ipaddress.ip_interface("10.10{}.0.{}/16".format(k, last)) for k in range(4)] if ib else [])
    h.pci = {"mgmt": "0009:01:00.0", "storage": "0009:01:00.1", "ib": ["000{}:03:00.0".format(i) for i in range(4)]}
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
        rec = nicmod.summarize(self.discover(gpus))
        self.assertEqual(nicmod.apply_plan(rec, host())["result"], "pass")
        self.assertEqual(rec["mgmt_if"], "enP9p1s0f0np0")
        self.assertEqual(rec["storage_mac"], "aa:bb:cc:00:00:02")
        self.assertEqual(rec["ib_devs"], ["mlx5_2", "mlx5_0", "mlx5_3", "mlx5_1"])
        checks = nicmod.check(rec, NETWORK, golden=rec)
        self.assertEqual(checks["cx8_port_active"]["result"], "fail")
        self.assertIn("rail2 DOWN", checks["cx8_port_active"]["reason"])
        self.assertEqual(checks["bf3_mode"]["result"], "skip")
        for name in ("gpus", "cx8_count", "cx8_link_layer", "bf3_ports", "lom", "golden_match", "gpu_rail_map"):
            self.assertEqual(checks[name]["result"], "pass", name)
        other = nicmod.summarize(self.discover(fake_sysfs(os.path.join(self.root, "b"), swap_gpu=True)))
        diff = nicmod.check(other, NETWORK, golden=rec)["golden_match"]
        self.assertEqual(diff["result"], "fail")
        self.assertIn("rail0", diff["reason"])

    def test_apply_plan(self):
        rec = nicmod.summarize(self.discover(fake_sysfs(self.root)))
        h = host()
        h.pci = {"mgmt": "0009:01:00.1", "storage": "0009:01:00.0",
                 "ib": ["0002:03:00.0", "0000:03:00.0", "0003:03:00.0", "0001:03:00.0"]}
        check = nicmod.apply_plan(rec, h)
        self.assertEqual(check["result"], "fail")                          # rails are in GPU order, not this one
        self.assertIn("compute device 0 PCI 0002:03:00.0 is rail 2 by GPU distance", check["reason"])
        self.assertEqual(rec["mgmt_if"], "enP9p1s0f1np1")
        h.pci["ib"] = ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]
        self.assertEqual(nicmod.apply_plan(rec, h), {"result": "pass", "reason": "all PCI addresses found"})
        h.pci["mgmt"] = "0009:01:00.5"
        self.assertIn("mgmt PCI 0009:01:00.5 has no network interface", nicmod.apply_plan(rec, h)["reason"])
        h.pci = {}
        self.assertIsNone(nicmod.apply_plan(rec, h))

    def test_plain_nic_roles(self):
        fake_sysfs(self.root)
        raw = self.mod.discover({"cx8_ids": ["1023"], "bf3_ids": ["a2dc"], "mlxconfig": False, "sysfs": self.root,
                                 "fake_gpus": [], "nic_pcis": ["0005:01:00.0", "0006:01:00.0"]})
        self.assertEqual(raw["nics"], [{"pci": "0005:01:00.0", "vendor": "8086", "device": "1533", "netdev": "eno1",
                                        "mac": "11:22:33:44:55:66"}])
        rec = nicmod.summarize(raw)
        h = host()
        h.pci = {"mgmt": "0005:01:00.0", "storage": None, "ib": []}
        self.assertEqual(nicmod.apply_plan(rec, h)["result"], "pass")
        self.assertEqual((rec["mgmt_if"], rec["mgmt_mac"]), ("eno1", "11:22:33:44:55:66"))

    def test_serial(self):
        fake_sysfs(self.root)
        w(os.path.join(self.root, "class/dmi/id/product_serial"), "SN-0001\n")
        self.assertEqual(nicmod.summarize(self.discover([]))["serial"], "SN-0001")
        w(os.path.join(self.root, "class/dmi/id/product_serial"), "To Be Filled By O.E.M.")
        self.assertEqual(nicmod.summarize(self.discover([]))["serial"], "")

    def test_bf3_mode(self):
        rec = nicmod.summarize(self.discover(fake_sysfs(self.root)))
        rec["bf3"].update({"mode": "DISABLED(1)", "mode_next": "DISABLED(1)"})
        self.assertEqual(nicmod.check(rec, NETWORK)["bf3_mode"]["result"], "pass")
        rec["bf3"].update({"mode": "ENABLED(0)", "mode_next": "DISABLED(1)"})
        c = nicmod.check(rec, NETWORK)["bf3_mode"]
        self.assertEqual(c["result"], "fail")
        self.assertIn("pending", c["reason"])


if __name__ == "__main__":
    unittest.main()
