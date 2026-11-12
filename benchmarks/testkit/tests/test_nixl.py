import os
import unittest

from mtest.checks import check_tests
from mtest.config import load_both
from mtest.executor import run_items
from mtest.parsers import nixl
from mtest.runners import nixl_case_args, nixl_pairs
from mtest.state import StateStore, read_json
from tests.helpers import Workspace
from tests.test_executor import FakeOps

H1, H2, H3 = "gb300-r1-t01", "gb300-r1-t02", "gb300-r1-t03"

HEAD = ("Block Size (B)      Batch Size     B/W (GB/Sec)   Avg Lat. (us)  Avg Prep (us)  P99 Prep (us)  "
        "Avg Post (us)  P99 Post (us)  Avg Tx (us)    P99 Tx (us)\n" + "-" * 80 + "\n")


def table(*rows):
    return HEAD + "".join("{:<20}{:<15}{:<15.6f}{:<15.1f}0.0            0.0\n".format(b, 1, bw, lat)
                          for b, bw, lat in rows)


def write_runs(raw, runs):
    os.makedirs(raw, exist_ok=True)
    for run, (rc, out, target) in runs.items():
        for suffix, text in ((".rc", "{}\n".format(rc)), (".txt", out), (".target.txt", target)):
            with open(os.path.join(raw, run + suffix), "w") as f:
                f.write(text)


class Node:

    def __init__(self, name, rack="R1"):
        self.host = type("H", (), {"name": name, "rack": rack, "tray": int(name[-2:]), "mgmt_ip": "10.0.0.1"})()


class NixlPlanTest(unittest.TestCase):

    def test_sequential_pairs_odd_count(self):
        nodes = [Node(h) for h in (H1, H2, H3)]
        pairs, notes = nixl_pairs(nodes)
        self.assertEqual([(a.host.name, b.host.name) for a, b in pairs], [(H1, H2), (H1, H3)])
        self.assertIn(H3, notes[0])

    def test_case_args(self):
        args = nixl_case_args("s5", {"device_list": "mlx5_0,mlx5_1", "case_args": {"s5": ["--num_threads", 4]}})
        self.assertIn("--check_consistency", args)
        self.assertEqual(args[args.index("--device_list") + 1], "mlx5_0,mlx5_1")
        self.assertEqual(args[-2:], ["--num_threads", "4"])
        storage = nixl_case_args("gds_read", {"filepath": "/mnt/nixl"})
        self.assertEqual(storage[storage.index("--filepath") + 1], "/mnt/nixl")
        self.assertIn("--storage_enable_direct", storage)
        self.assertNotIn("--target_seg_type", storage)


class NixlParseTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=1)
        self.raw = os.path.join(self.ws.dir, "raw")

    def tearDown(self):
        self.ws.close()

    def test_local_metrics_and_limits(self):
        write_runs(self.raw, {"s1": (0, table((4096, 1.5, 3.2), (67108864, 320.0, 210.0)), ""),
                              "s2": (0, table((4096, 1.0, 4.0)), "Consistency check failed\n")})
        per_host, extra = nixl.parse_group(self.ws.dir, {"min_bw_gbps": {"s1": 300}}, H1, ["s1", "s2"])
        self.assertEqual(per_host[H1]["metrics"]["nixl/s1/bw_gbps"]["value"], 320.0)
        self.assertEqual(per_host[H1]["metrics"]["nixl/s1/lat_us"]["value"], 3.2)
        self.assertEqual(extra["status"], "fail")
        self.assertIn("s2: Consistency check failed", extra["reason"])
        _, extra = nixl.parse_group(self.ws.dir, {"min_bw_gbps": {"s1": 400}}, H1, ["s1"])
        self.assertIn("s1: 320.00 GB/s < 400", extra["reason"])

    def test_pair_failure_and_missing_run_blame_their_hosts(self):
        ok = table((4096, 2.0, 4.1), (1048576, 45.0, 23.0))
        write_runs(self.raw, {"s3@{}+{}".format(H1, H2): (0, ok, ""), "s3@{}+{}".format(H1, H3): (124, "", "")})
        expected = ["s3@{}+{}".format(H1, H2), "s3@{}+{}".format(H1, H3), "s4@{}+{}".format(H1, H2)]
        per_host, extra = nixl.parse_group(self.ws.dir, {}, H1, expected)
        self.assertEqual(sorted(per_host), [H1, H2, H3])
        self.assertTrue(per_host[H3]["own"])
        self.assertIn("exit timeout", per_host[H3]["reason"])
        self.assertIn("s4 not run", per_host[H2]["reason"])
        self.assertEqual(extra["tables"][0]["rows"][0][-1], "pass")
        self.assertEqual(len(extra["tables"][0]["rows"]), 3)

    def test_etcd_failed(self):
        os.makedirs(self.raw)
        open(os.path.join(self.raw, "etcd-failed"), "w").close()
        _, extra = nixl.parse_group(self.ws.dir, {}, H1, ["s1"])
        self.assertEqual(extra["status"], "error")


TESTS = """
images: {nixl_benchmark: "nixl:1"}
image_pull: skip
runtime: {results_dir: ./runs, poll_interval: 0.01}
nixl:
  common: {image: nixl_benchmark}
  pair: {enabled: true, scope: group, hosts: all, cases: [s3]}
  storage: {enabled: false, scope: node, hosts: all}
"""


class NixlFlowTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.ws.write("tests.yaml", TESTS)
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def load(self):
        cluster, tests, problems = load_both(self.ws.cluster, self.ws.tests)
        check_tests(tests, cluster, problems)
        return cluster, tests, problems

    def test_checks(self):
        self.ws.edit("tests.yaml", "cases: [s3]", "cases: [s3, s1, s9]")
        self.ws.edit("tests.yaml", "storage: {enabled: false", "storage: {enabled: true")
        errors = self.load()[2].errors
        self.assertIn("nixl.pair: case s1 is a local case, not pair", errors)
        self.assertTrue(any(e.startswith("nixl.pair: unknown case s9") for e in errors))
        self.assertIn("nixl.storage: filepath is not set (a directory on the host)", errors)

    def test_pair_flow(self):
        cluster, tests, problems = self.load()
        self.assertEqual(problems.errors, [])
        ok = table((4096, 2.0, 4.1), (67108864, 45.0, 1490.0))
        files = {"raw/s3@{}+{}.rc".format(H1, H2): "0\n", "raw/s3@{}+{}.txt".format(H1, H2): ok,
                 "raw/s3@{}+{}.rc".format(H1, H3): "1\n", "raw/s3@{}+{}.txt".format(H1, H3): "UCX error\n"}
        behave = {h: {"polls": 10 ** 6} for h in (H1, H2, H3)}
        behave[H1]["files"] = files
        ops = FakeOps(behave)
        run_id, code = run_items(cluster, tests, ["nixl.pair"], None, None, ops, out=self.lines.append)
        launcher = [c for c in ops.calls if c[0] == "exec"]
        self.assertEqual(launcher[0][2], "bash /mtest/nixl_run.sh")
        tools = sorted(os.path.basename(c[2]) for c in ops.calls if c[0] == "put" and c[1] == H2 and ".mtest/" in c[2])
        self.assertEqual(tools, ["nixl_cases.tsv", "nixl_pairs.tsv", "nixl_run.sh"])
        st = StateStore(tests.results_dir).load()["hosts"]
        self.assertEqual(st[H3]["nixl.pair"]["status"], "fail", self.lines)
        self.assertFalse(st[H3]["nixl.pair"]["group_result"])
        self.assertTrue(st[H2]["nixl.pair"]["group_result"])
        g = read_json(os.path.join(tests.results_dir, run_id, "_groups", "nixl.pair", "R1", "summary.json"))
        self.assertEqual([r[-1] for r in g["tables"][0]["rows"]], ["pass", "fail: exit 1; no result table"])


if __name__ == "__main__":
    unittest.main()
