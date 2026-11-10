import os
import unittest

from mtest.checks import check_tests, enabled_items
from mtest.config import load_both
from mtest.plan import build_plan, format_plan
from mtest.state import nic_path, write_json
from tests.helpers import Workspace


def load(ws):
    cluster, tests, problems = load_both(ws.cluster, ws.tests)
    check_tests(tests, cluster, problems)
    return cluster, tests, problems


class ChecksTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.close()

    def test_template_is_valid(self):
        _, tests, problems = load(self.ws)
        self.assertEqual(problems.errors, [])
        self.assertEqual(problems.warnings, [])
        self.assertEqual([i.id for i, _ in enabled_items(tests)],
                         ["dcgm", "superbench.single_node", "superbench.pair", "superbench.rack", "imex",
                          "sharp.scale_up", "moe.single", "moe.rack"])

    def errors(self):
        return load(self.ws)[2]

    def test_bad_selector_and_vars(self):
        self.ws.edit("tests.yaml", "hosts: [gb300-r1-t01, gb300-r1-t02]", "hosts: [gb300-r1-t01, R7]")
        self.ws.edit("tests.yaml", 'TOPK: "8"', 'TOPK: "{topk}"')
        errors = self.errors().errors
        self.assertIn("superbench.pair: 'R7' is not a host, rack, tag:<name> or all", errors)
        self.assertIn("moe.rack.env: unknown template variables ['topk']", errors)

    def test_missing_image(self):
        self.ws.edit("tests.yaml", "  moe_bench: registry.local/metal-deployer/moe-bench:latest\n", "")
        self.assertIn("moe.single: image 'moe_bench' is not set under images", self.errors().errors)

    def test_sharp_falls_back_to_worker(self):
        cluster, tests, problems = load(self.ws)
        item, cfg = [x for x in enabled_items(tests) if x[0].id == "sharp.scale_up"][0]
        self.assertEqual(tests.image_for(item, cfg), "superbench/main:cuda13.0")

    def test_runtime_and_scope(self):
        self.ws.edit("tests.yaml", "  run_timeout: 7200", "  run_timeout: 0")
        self.ws.edit("tests.yaml", "  group_order: serial", "  group_order: random")
        self.ws.edit("tests.yaml", "dcgm:\n  enabled: true\n  scope: node", "dcgm:\n  enabled: true\n  scope: pod")
        errors = self.errors().errors
        self.assertIn("runtime.run_timeout must be a positive integer", errors)
        self.assertIn("runtime.group_order must be serial or parallel", errors)
        self.assertIn("dcgm: scope must be node, rack or group", errors)

    def test_warnings(self):
        self.ws.edit("tests.yaml", "imex:\n  enabled: true", "imex:\n  enabled: false")
        self.ws.edit("tests.yaml", "  scale_out:                      # IB SHARP across racks; needs >= 2 racks and SHARP AM in UFM\n    enabled: false",
                     "  scale_out:\n    enabled: true")
        self.ws.edit("tests.yaml", "    groups: [[R1, R2]]\n    sharp_env", "    groups: [[R1]]\n    sharp_env")
        self.ws.edit("tests.yaml", "    per_rank: {}", "    per_rank: {first: {}}")
        problems = self.errors()
        self.assertTrue(any("use MNNVL but imex is not enabled" in w for w in problems.warnings))
        self.assertIn("sharp.scale_out: group R1 has only one rack; IB SHARP needs at least 2", problems.warnings)
        self.assertIn("moe.single.per_rank: key 'first' must be a rank number", problems.errors)

    def test_removed_section_is_disabled(self):
        text = self.ws.read("tests.yaml")
        start = text.index("# ---------------- S8 MoE")
        end = text.index("# ---------------- S9 report")
        self.ws.write("tests.yaml", text[:start] + text[end:])
        _, tests, problems = load(self.ws)
        self.assertEqual(problems.errors, [])
        self.assertNotIn("moe.single", [i.id for i, _ in enabled_items(tests)])


class PlanTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.close()

    def test_moe_rack_plan(self):
        cluster, tests, _ = load(self.ws)
        plans = build_plan(cluster, tests, ["moe.rack"])
        self.assertEqual(len(plans), 1)
        p = plans[0]
        self.assertEqual([g.id for g in p.groups], ["R1", "R2"])
        r2 = [n for n in p.nodes if n.group.id == "R2"]
        self.assertEqual(r2[5].env["RANK"], "5")
        self.assertEqual(r2[5].env["WORLD_SIZE"], "18")
        self.assertEqual(r2[5].env["MASTER_ADDR"], "10.20.1.11")
        self.assertEqual(r2[5].env["MASTER_PORT"], "29501")
        self.assertEqual(r2[5].cmd, ["moe-bench", "--log-dir", "/results", "--suite", "hybrid-ep"])
        text = format_plan(plans, 29500)
        self.assertIn("group R2: 18 hosts, master gb300-r2-t01 10.20.1.11:29501", text)
        self.assertIn("differs per node: RANK", text)
        self.assertIn("<S3:mgmt_if>", text)

    def test_nic_results_fill_variables(self):
        cluster, tests, _ = load(self.ws)
        write_json(nic_path(tests.results_dir, "gb300-r1-t01"),
                   {"mgmt_if": "enP2p1s0f0np0", "ib_devs": ["mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3"]})
        plans = build_plan(cluster, tests, ["dcgm"], only="gb300-r1-t01")
        env = plans[0].nodes[0].env
        self.assertEqual(env["NCCL_SOCKET_IFNAME"], "enP2p1s0f0np0")
        self.assertEqual(env["NCCL_IB_HCA"], "mlx5_0,mlx5_1,mlx5_2,mlx5_3")
        self.assertNotIn("<S3:", format_plan(plans, 29500))
        self.assertTrue(os.path.isfile(nic_path(tests.results_dir, "gb300-r1-t01")))

    def test_selectors_keep_run_order(self):
        cluster, tests, _ = load(self.ws)
        plans = build_plan(cluster, tests, ["moe", "dcgm"])
        self.assertEqual([p.item.id for p in plans], ["dcgm", "moe.single", "moe.rack"])
        text = format_plan(plans, 29500, verbose=True)
        self.assertEqual(text.count("(rank 0)"), 36 + 36 + 2)


if __name__ == "__main__":
    unittest.main()
