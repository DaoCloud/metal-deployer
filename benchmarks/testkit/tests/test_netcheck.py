import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from testkit import netcheck, scaffold
from testkit.cli import main
from tests.test_nicnet import load_node, w


def load(text):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "network.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return netcheck.load_network(path)


class FileCheckTest(unittest.TestCase):

    def setUp(self):
        self.text = scaffold.network_text(1, 3)

    def test_template_is_clean(self):
        net, p = load(self.text)
        self.assertEqual((p.errors, p.warnings), ([], []))
        t1 = net.hosts[0]
        self.assertEqual((t1.ssh, t1.rack, t1.tray), ("10.10.0.11", "R1", 1))
        self.assertEqual([port.label for port in t1.ports], ["mgmt", "storage", "rail0", "rail1", "rail2", "rail3"])
        self.assertEqual(t1.default, ("mgmt", "10.20.0.1"))
        self.assertEqual(t1.routes, [("storage", "10.31.0.0/16", "10.30.0.1")])
        self.assertEqual(t1.rails()[0].pkey, "0x7fff")

    def test_errors(self):
        text = (self.text.replace('pci: "0009:01:00.1"', "pci: 0009:01:00.1", 1)        # unquoted PCI
                .replace("ip: 10.30.0.12/16", "ip: 10.30.0.11/16")                      # duplicate address
                .replace("10.101.0.12/16", "10.109.0.12/16")                            # rail1 in another subnet
                .replace("gateway: 10.20.0.1,", "gateway: 10.21.0.1,", 1)               # gateway outside subnet
                .replace("lom: {ip: 10.10.0.12/16", "lom: {ip: 10.10.0.12")             # no prefix
                .replace("tray: 3", "tray: 2"))                                         # same slot
        _, p = load(text)
        msgs = "\n".join(p.errors)
        self.assertIn("gb300-r1-t01.storage.pci: PCI address must be quoted", msgs)
        self.assertIn("address 10.30.0.11 is used by gb300-r1-t01.storage, gb300-r1-t02.storage", msgs)
        self.assertIn("compute rail1: gb300-r1-t02 in 10.109.0.0/16", msgs)
        self.assertIn("gb300-r1-t01.mgmt.gateway: gateway 10.21.0.1 is outside 10.20.0.0/16", msgs)
        self.assertIn("gb300-r1-t02.lom.ip: '10.10.0.12' must be an address with prefix", msgs)
        self.assertIn("hosts.gb300-r1-t02 and hosts.gb300-r1-t03: both are rack R1 tray 2", msgs)

    def test_differs_from_most_hosts_is_a_warning(self):
        text = self.text.replace('{pci: "0001:03:00.0", ip: 10.101.0.12/16}',
                                 '{pci: "0005:03:00.0", ip: 10.101.0.12/16, mtu: 2044}')
        _, p = load(text + "    x-qos: {trust: dscp}\n    tags: [golden]\n")
        self.assertEqual(p.errors, [])
        msgs = "\n".join(p.warnings)
        self.assertIn("compute.devices[1].pci: gb300-r1-t02 0005:03:00.0 differs from most hosts (0001:03:00.0)", msgs)
        self.assertIn("compute.devices[1].mtu: gb300-r1-t02 2044 differs", msgs)
        self.assertIn("hosts.gb300-r1-t03: unknown key tags", msgs)
        self.assertNotIn("x-qos", msgs)

    def test_node_args_and_selection(self):
        net, _ = load(self.text)
        t1, t2, t3 = net.hosts
        args = netcheck.node_args(t1, net.hosts)
        self.assertEqual(args["default"], {"label": "mgmt", "gateway": "10.20.0.1"})
        pings = {p["name"]: p["target"] for p in args["pings"]}
        self.assertEqual(pings["mgmt-gw"], "10.20.0.1")
        self.assertEqual(pings["storage-gw"], "10.30.0.1")
        self.assertEqual(pings["rail3-peer"], "10.103.0.12")
        self.assertEqual(netcheck.node_args(t1, net.hosts, ping=False)["pings"], [])
        self.assertEqual([h.name for h in netcheck.select(net.hosts, "R1", exclude=t1.name)], [t2.name, t3.name])
        with self.assertRaises(netcheck.ConfigError):
            netcheck.select(net.hosts, "R9")


class NodeCheckTest(unittest.TestCase):
    """node_scripts/net_check.py against a fake /sys and canned "ip -j" output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.mod = load_node("net_check.py")
        self.mod.ROOT = self.root
        for pci, dev, ib, ll in (("0000:03:00.0", "eth1", "mlx5_4", "Ethernet"),
                                 ("0000:01:00.0", "ibp1s0", "mlx5_0", "InfiniBand")):
            base = os.path.join(self.root, "bus/pci/devices", pci)
            os.makedirs(os.path.join(base, "net", dev))
            os.makedirs(os.path.join(base, "infiniband", ib))
            w(os.path.join(self.root, "class/infiniband", ib, "ports/1/link_layer"), ll)
            w(os.path.join(self.root, "class/infiniband", ib, "ports/1/state"), "4: ACTIVE")
        w(os.path.join(self.root, "class/net/ibp1s0/pkey"), "0xffff")
        self.links = {
            "eth0": {"ifname": "eth0", "mtu": 1500, "flags": ["UP"], "operstate": "UP",
                     "addr_info": [{"family": "inet", "local": "10.10.0.11", "prefixlen": 24}]},
            "eth1": {"ifname": "eth1", "mtu": 1500, "flags": ["UP"], "operstate": "UP",
                     "addr_info": [{"family": "inet", "local": "10.20.0.11", "prefixlen": 24}]},
            "ibp1s0": {"ifname": "ibp1s0", "mtu": 4092, "flags": ["UP"], "operstate": "UP",
                       "link_type": "infiniband",
                       "addr_info": [{"family": "inet", "local": "10.100.0.11", "prefixlen": 24}]},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def port(self, **kw):
        port = {"label": "mgmt", "pci": "0000:03:00.0", "ip": "10.20.0.11/24", "mtu": 1500,
                "link_layer": "ethernet", "pkey": None}
        port.update(kw)
        return port

    def check(self, port):
        c = self.mod.Checks()
        dev = self.mod.check_port(c, port, self.links)
        return dev, c.items[0]

    def test_port_pass(self):
        dev, item = self.check(self.port())
        self.assertEqual((dev, item["result"]), ("eth1", "pass"))
        dev, item = self.check(self.port(label="rail0", pci="0000:01:00.0", ip="10.100.0.11/24", mtu=4092,
                                         link_layer="infiniband", pkey="0x7fff"))
        self.assertEqual((dev, item["result"]), ("ibp1s0", "pass"), item)

    def test_port_failures(self):
        _, item = self.check(self.port(pci="0000:09:00.0"))
        self.assertIn("PCI 0000:09:00.0 not found", item["detail"])
        _, item = self.check(self.port(mtu=9000, link_layer="infiniband"))
        self.assertIn("mtu 1500 (expected 9000)", item["detail"])
        self.assertIn("link layer ethernet (expected infiniband)", item["detail"])
        _, item = self.check(self.port(ip="10.10.0.11/24"))
        self.assertIn("address 10.10.0.11/24 missing (has 10.20.0.11/24); it is on eth0", item["detail"])
        _, item = self.check(self.port(label="rail0", pci="0000:01:00.0", ip="10.100.0.11/24", mtu=4092,
                                       link_layer="infiniband", pkey="0x0002"))
        self.assertIn("pkey 0xffff (expected 0x0002)", item["detail"])
        self.links["eth1"]["flags"] = []
        _, item = self.check(self.port())
        self.assertEqual(item["result"], "fail")

    def test_routes_and_policy(self):
        def ip_json(*args):
            table = {("route", "show", "default"): [{"dst": "default", "gateway": "10.20.0.1", "dev": "eth1"}],
                     ("route", "show", "10.31.0.0/16"): [{"dst": "10.31.0.0/16", "gateway": "10.30.0.1",
                                                          "dev": "eth2"}],
                     ("rule", "show"): [{"priority": 100, "src": "10.10.0.11", "table": "100"}],
                     ("route", "show", "table", "100"): [{"dst": "default", "gateway": "10.10.0.1", "dev": "eth0"}]}
            return table.get(args, [])

        c = self.mod.Checks()
        with mock.patch.object(self.mod, "ip_json", side_effect=ip_json), \
                mock.patch.dict(os.environ, {"SSH_CONNECTION": ""}):
            self.mod.check_lom(c, {"ip": "10.10.0.11/24", "gateway": "10.10.0.1",
                                   "policy": {"table": 100, "priority": 100}}, self.links)
            devs = {"mgmt": "eth1", "storage": "eth2"}
            self.mod.check_default(c, {"label": "mgmt", "gateway": "10.20.0.1"}, devs)
            self.mod.check_route(c, {"label": "storage", "to": "10.31.0.0/16", "via": "10.30.0.1"}, devs)
            self.mod.check_route(c, {"label": "storage", "to": "10.32.0.0/16", "via": "10.30.0.1"}, devs)
            self.mod.check_lom(c, {"ip": "10.10.0.11/24", "gateway": "10.10.0.1",
                                   "policy": {"table": 101, "priority": 100}}, self.links)
        result = [(i["name"], i["result"]) for i in c.items]
        self.assertEqual(result, [("lom", "pass"), ("lom_policy", "pass"), ("default_route", "pass"),
                                  ("route:storage 10.31.0.0/16", "pass"), ("route:storage 10.32.0.0/16", "fail"),
                                  ("lom", "pass"), ("lom_policy", "fail")])


class CliTest(unittest.TestCase):

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_static(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli(["init", "--dir", d])
            self.assertEqual(code, 0, out)
            path = os.path.join(d, "network.yaml")
            code, out = self.run_cli(["-n", path, "net-check", "--static"])
            self.assertEqual(code, 0, out)
            self.assertIn("2 hosts, 2 selected; 0 error(s)", out)
            with open(path, "a", encoding="utf-8") as f:
                f.write("  gb300-r1-t03:\n    rack: R1\n")
            code, out = self.run_cli(["-n", path, "net-check", "--static"])
            self.assertEqual(code, 2, out)
            self.assertIn("hosts.gb300-r1-t03.lom is required", out)
            self.assertEqual(self.run_cli(["-n", os.path.join(d, "missing.yaml"), "net-check"])[0], 1)


if __name__ == "__main__":
    unittest.main()
