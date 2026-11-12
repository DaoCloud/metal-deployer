import unittest

from testkit.config import load_cluster, load_tests_file
from testkit.util import ConfigError, Problems
from tests.helpers import Workspace


class ClusterTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.close()

    def load(self):
        problems = Problems()
        return load_cluster(self.ws.cluster, problems), problems

    def test_generated_cluster_is_valid(self):
        cluster, problems = self.load()
        self.assertEqual(problems.errors, [])
        self.assertEqual(problems.warnings, [])
        self.assertEqual(len(cluster.hosts), 36)
        self.assertEqual(cluster.racks, ["R1", "R2"])
        host = cluster.by_name["gb300-r1-t01"]
        self.assertTrue(host.golden)
        self.assertEqual(host.mgmt_ip, "10.20.0.11")
        self.assertEqual(len(host.ib), 4)
        self.assertEqual(host.gpus, 4)

    def test_rank_order_is_rack_then_tray(self):
        ws = Workspace(racks=10, trays=2)
        try:
            cluster = load_cluster(ws.cluster, Problems())
            order = [(h.rack, h.tray) for h in cluster.hosts]
            self.assertEqual(order[:4], [("R1", 1), ("R1", 2), ("R2", 1), ("R2", 2)])
            self.assertEqual(order[-2:], [("R10", 1), ("R10", 2)])
        finally:
            ws.close()

    def test_defaults_env_is_merged(self):
        self.ws.edit("cluster.yaml", "  env: {}", "  env: {A: '1', B: '2'}")
        self.ws.edit("cluster.yaml", "    # env: {NCCL_DEBUG: INFO}", "    env: {B: '3', N: 5}")
        cluster, problems = self.load()
        self.assertEqual(problems.errors, [])
        self.assertEqual(cluster.by_name["gb300-r1-t02"].env, {"A": "1", "B": "3", "N": "5"})
        self.assertEqual(cluster.by_name["gb300-r1-t03"].env, {"A": "1", "B": "2"})

    def assertError(self, fragment):
        _, problems = self.load()
        self.assertTrue(any(fragment in e for e in problems.errors),
                        "no error containing {!r} in {}".format(fragment, problems.errors))

    def test_duplicate_name(self):
        self.ws.edit("cluster.yaml", "name: gb300-r1-t02", "name: gb300-r1-t01")
        self.assertError("duplicate host name")

    def test_duplicate_slot(self):
        self.ws.edit("cluster.yaml", "    tray: 2\n", "    tray: 1\n")
        self.assertError("both are R1 tray 1")

    def test_duplicate_address(self):
        self.ws.edit("cluster.yaml", "ssh: 10.10.0.12", "ssh: 10.10.0.11")
        self.assertError("address 10.10.0.11 is used by")

    def test_missing_mgmt(self):
        self.ws.edit("cluster.yaml", "    mgmt: 10.20.0.12/16\n", "")
        self.assertError("mgmt is required")

    def test_address_without_prefix(self):
        self.ws.edit("cluster.yaml", "mgmt: 10.20.0.12/16", "mgmt: 10.20.0.12")
        self.assertError("needs a prefix length")

    def test_bad_address(self):
        self.ws.edit("cluster.yaml", "storage: 10.30.0.12/16", "storage: 10.30.0.312/16")
        self.assertError("is not a valid address")

    def test_ib_count(self):
        self.ws.edit("cluster.yaml", ", 10.103.0.12/16]", "]")
        self.assertError("ib has 3 addresses, expected 4")

    def test_rail_subnet(self):
        self.ws.edit("cluster.yaml", "10.101.1.15/16", "10.111.1.15/16")
        self.assertError("rail 1: IPoIB addresses are in different subnets")

    def test_gateway_subnet(self):
        self.ws.edit("cluster.yaml", "mgmt: 10.20.1.18/16", "mgmt: 10.21.1.18/16")
        self.assertError("not in the same subnet as gateway")

    def test_two_golden(self):
        self.ws.edit("cluster.yaml", "    # skip: [moe]", "    tags: [golden]")
        self.assertError("more than one golden host")

    def test_bad_tray(self):
        self.ws.edit("cluster.yaml", "    tray: 2\n", "    tray: two\n")
        self.assertError("tray must be a positive integer")

    def test_network_options(self):
        self.ws.edit("cluster.yaml", "  ipoib: {mtu: 4092, pkey: \"0x7fff\"}", "  ipoib: {mtu: 4092, pkey: \"0x8001\"}")
        self.ws.edit("cluster.yaml", "  match_by: mac ", "  match_by: pci ")
        self.ws.edit("cluster.yaml", "  rollback_seconds: 120", "  rollback_seconds: 5")
        self.ws.edit("cluster.yaml", "routes: [10.30.0.0/16]", "routes: [10.30.0.0/33]")
        self.ws.edit("cluster.yaml", "    table: 100", "    table: 254")
        self.ws.edit("cluster.yaml", "  profile: gb300", "  profile: b200")
        self.ws.edit("cluster.yaml", "mgmt: {device: bf3", "mgmt: {device: dpu")
        _, problems = self.load()
        for frag in ("only the default partition", "match_by must be", "rollback_seconds must be",
                     "'10.30.0.0/33' is not a valid subnet", "network.lom.table must be", "network.profile must be",
                     "network.mgmt.device must be bf3 or nic or lom"):
            self.assertTrue(any(frag in e for e in problems.errors), frag)

    def test_unquoted_pci(self):
        self.ws.edit("cluster.yaml", 'mgmt_pci: "0009:01:00.0"', "mgmt_pci: 0009:01:00.0")
        self.assertError("must be quoted in YAML")

    def test_unknown_key_warns(self):
        self.ws.edit("cluster.yaml", "    # skip: [moe]", "    colour: red")
        self.ws.edit("cluster.yaml", "  port: 22", "  port: 22\n  prot: 22")
        _, problems = self.load()
        self.assertIn("cluster.yaml: unknown key hosts[gb300-r1-t02].colour", problems.warnings)
        self.assertIn("cluster.yaml: unknown key ssh.prot", problems.warnings)

    def test_unreadable(self):
        self.ws.write("cluster.yaml", "hosts: [\n")
        with self.assertRaises(ConfigError):
            self.load()
        self.ws.write("cluster.yaml", "- a\n")
        with self.assertRaises(ConfigError):
            self.load()


class TestsFileTest(unittest.TestCase):

    def test_runtime_defaults_and_results_dir(self):
        ws = Workspace(1, 2)
        try:
            ws.edit("tests.yaml", "  results_dir: ./runs", "  results_dir: out/runs")
            ws.edit("tests.yaml", "  run_timeout: 7200               # seconds, per group\n", "")
            problems = Problems()
            tests = load_tests_file(ws.tests, problems)
            self.assertEqual(problems.warnings, [])
            self.assertEqual(tests.results_dir, ws.dir + "/out/runs")
            self.assertEqual(tests.runtime["run_timeout"], 7200)
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
