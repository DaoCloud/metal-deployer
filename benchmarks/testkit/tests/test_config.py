import unittest

from testkit.config import load_both, load_tests_file
from testkit.util import ConfigError, Problems
from tests.helpers import Workspace


class ClusterTest(unittest.TestCase):
    """Hosts from network.yaml plus the host settings of tests.yaml."""

    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.close()

    def load(self):
        cluster, _, problems = load_both(self.ws.network, self.ws.tests)
        return cluster, problems

    def assertError(self, fragment):
        _, problems = self.load()
        self.assertTrue(any(fragment in e for e in problems.errors),
                        "no error containing {!r} in {}".format(fragment, problems.errors))

    def test_generated_cluster_is_valid(self):
        cluster, problems = self.load()
        self.assertEqual(problems.errors, [])
        self.assertEqual(problems.warnings, [])
        self.assertEqual(len(cluster.hosts), 36)
        self.assertEqual(cluster.racks, ["R1", "R2"])
        host = cluster.by_name["gb300-r1-t01"]
        self.assertTrue(host.golden)
        self.assertEqual((host.ssh, host.mgmt_ip, str(host.storage)), ("10.10.0.11", "10.20.0.11", "10.30.0.11/16"))
        self.assertEqual([str(i) for i in host.ib][:2], ["10.100.0.11/16", "10.101.0.11/16"])
        self.assertEqual(host.pci, {"mgmt": "0009:01:00.0", "storage": "0009:01:00.1",
                                    "ib": ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]})
        self.assertEqual(host.gpus, 4)

    def test_rank_order_is_rack_then_tray(self):
        ws = Workspace(racks=10, trays=2)
        try:
            cluster, _, _ = load_both(ws.network, ws.tests)
            order = [(h.rack, h.tray) for h in cluster.hosts]
            self.assertEqual(order[:4], [("R1", 1), ("R1", 2), ("R2", 1), ("R2", 2)])
            self.assertEqual(order[-2:], [("R10", 1), ("R10", 2)])
        finally:
            ws.close()

    def test_without_rack_and_tray(self):
        ws = Workspace(racks=1, trays=3)
        try:
            text = ws.read("network.yaml")
            for t in (1, 2, 3):
                text = text.replace("    rack: R1\n    tray: {}\n".format(t), "")
            ws.write("network.yaml", text.replace("gb300-r1-t01:", "gb300-r1-t10:"))
            ws.edit("tests.yaml", "golden: gb300-r1-t01", "golden: gb300-r1-t02")
            cluster, _, problems = load_both(ws.network, ws.tests)
            self.assertEqual((problems.errors, problems.warnings), ([], []))
            self.assertEqual(cluster.racks, ["all"])
            self.assertEqual([h.name for h in cluster.hosts], ["gb300-r1-t02", "gb300-r1-t03", "gb300-r1-t10"])
        finally:
            ws.close()

    def test_host_settings_from_tests_yaml(self):
        self.ws.edit("tests.yaml", "hosts: {}", "hosts:\n  gb300-r1-t02: {tags: [batch1], env: {B: '3', N: 5}, "
                                               "skip: [moe], colour: red}")
        cluster, problems = self.load()
        self.assertEqual(problems.errors, [])
        self.assertIn("tests.yaml: unknown key hosts.gb300-r1-t02.colour", problems.warnings)
        h = cluster.by_name["gb300-r1-t02"]
        self.assertEqual((h.tags, h.env, h.skip), (["batch1"], {"B": "3", "N": "5"}, ["moe"]))
        self.assertEqual(cluster.by_name["gb300-r1-t03"].env, {})

    def test_unknown_hosts_in_tests_yaml(self):
        self.ws.edit("tests.yaml", "hosts: {}", "hosts: {gb300-r9-t01: {skip: [moe]}}")
        self.ws.edit("tests.yaml", "golden: gb300-r1-t01", "golden: gb300-r9-t02")
        _, problems = self.load()
        self.assertIn("tests.yaml: hosts.gb300-r9-t01 is not a host of network.yaml", problems.errors)
        self.assertIn("tests.yaml: golden gb300-r9-t02 is not a host of network.yaml", problems.errors)

    def test_no_golden_warns(self):
        self.ws.edit("tests.yaml", "golden: gb300-r1-t01", "")
        cluster, problems = self.load()
        self.assertEqual(problems.errors, [])
        self.assertFalse(any(h.golden for h in cluster.hosts))
        self.assertTrue(any("no golden host" in w for w in problems.warnings))

    def test_two_golden(self):
        self.ws.edit("tests.yaml", "hosts: {}", "hosts: {gb300-r1-t02: {tags: [golden]}}")
        self.assertError("more than one golden host")

    def test_missing_mgmt(self):
        self.ws.edit("network.yaml", '    mgmt: {pci: "0009:01:00.0", ip: 10.20.0.12/16', "    x-mgmt: {ip: 10.20.0.12/16")
        self.assertError("gb300-r1-t02: mgmt is required")

    def test_ib_count(self):
        self.ws.edit("tests.yaml", "cx8: {count: 4,", "cx8: {count: 8,")
        self.assertError("gb300-r1-t01: 4 compute devices, expected 8")

    def test_network_file_errors_are_reported(self):
        self.ws.edit("network.yaml", "lom: {ip: 10.10.0.12/16", "lom: {ip: 10.10.0.11/16")
        self.ws.edit("network.yaml", "tray: 2", "tray: 1")
        self.assertError("network.yaml: address 10.10.0.11 is used by gb300-r1-t01.lom, gb300-r1-t02.lom")
        self.assertError("network.yaml: hosts.gb300-r1-t01 and hosts.gb300-r1-t02: both are rack R1 tray 1")

    def test_platform(self):
        cluster, _ = self.load()
        self.assertEqual(cluster.platform, "gb300")
        self.ws.edit("tests.yaml", "platform: gb300 ", "platform: b300 ")
        _, problems = self.load()
        self.assertTrue(any("tests.yaml: platform 'b300' is not supported" in e for e in problems.errors))
        self.ws.edit("tests.yaml", "platform: b300 ", "")
        _, problems = self.load()
        self.assertTrue(any("tests.yaml: platform is required" in e for e in problems.errors))

    def test_unreadable(self):
        self.ws.write("network.yaml", "hosts: [\n")
        with self.assertRaises(ConfigError):
            self.load()
        self.ws.write("network.yaml", "- a\n")
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
