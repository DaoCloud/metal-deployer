import ipaddress
import unittest

from testkit.config import Host
from testkit.render import nic_vars, node_cmd, node_env, node_vars, render, unknown_vars
from testkit.selection import Group


def host(name, rack, tray, last, env=None):
    return Host(name, rack, tray, "10.10.0.{}".format(last), mgmt=ipaddress.ip_interface("10.20.0.{}/16".format(last)),
                ib=[ipaddress.ip_interface("10.10{}.0.{}/16".format(k, last)) for k in range(4)], env=env)


class RenderTest(unittest.TestCase):

    def setUp(self):
        self.a = host("gb300-r1-t01", "R1", 1, 11)
        self.b = host("gb300-r1-t02", "R1", 2, 12, env={"NCCL_DEBUG": "INFO", "X": "host"})
        self.group = Group("R1", [self.a, self.b], 3)

    def test_unknown_vars(self):
        self.assertEqual(unknown_vars({"A": "{nnodes}-{topk}", "B": ["{rank}", 5]}), {"topk", "rank"})
        self.assertEqual(unknown_vars("plain $HOME {mgmt_if}"), set())

    def test_render(self):
        self.assertEqual(render("{a}-{b}", {"a": 1}), "1-{b}")
        self.assertEqual(render("{mgmt_if}", {}), "<S3:mgmt_if>")
        self.assertEqual(render(8, {}), "8")
        self.assertEqual(render(True, {}), "true")

    def test_node_vars(self):
        v = node_vars("r1", self.group, 1, self.b, 29503, {"mgmt_if": "enp1", "ib_devs": ["mlx5_0", "mlx5_1"]})
        self.assertEqual(v["master_addr"], "10.20.0.11")
        self.assertEqual(v["nnodes"], 2)
        self.assertEqual(v["node_list"], "10.20.0.11,10.20.0.12")
        self.assertEqual(v["ib2_ip"], "10.102.0.12")
        self.assertEqual(v["ib_hcas"], "mlx5_0,mlx5_1")
        self.assertEqual(v["ib_ucx_devs"], "mlx5_0:1,mlx5_1:1")
        self.assertEqual(v["mgmt_if"], "enp1")
        self.assertEqual(nic_vars(None), {})

    def test_env_layers(self):
        common = {"X": "common", "IF": "{mgmt_if}"}
        cfg = {"env": {"X": "item", "RANK": "{node_rank}"},
               "per_rank": {"1": {"env": {"X": "rank1", "ONLY1": "y"}}}}
        v0 = node_vars("r", self.group, 0, self.a, 1, None)
        v1 = node_vars("r", self.group, 1, self.b, 1, None)
        e0 = node_env(common, cfg, 0, self.a, v0)
        e1 = node_env(common, cfg, 1, self.b, v1)
        self.assertEqual(e0, {"X": "item", "IF": "<S3:mgmt_if>", "RANK": "0"})
        self.assertEqual(e1["X"], "host")
        self.assertEqual(e1["ONLY1"], "y")
        self.assertEqual(e1["NCCL_DEBUG"], "INFO")
        self.assertEqual(e1["RANK"], "1")

    def test_cmd(self):
        cfg = {"cmd": ["moe-bench", "--log-dir", "/results/{group_id}"], "args": ["--suite", "x"],
               "per_rank": {0: {"args_append": ["--tests", "layout"]}}}
        v = node_vars("r", self.group, 0, self.a, 1, None)
        self.assertEqual(node_cmd(cfg, 0, v), ["moe-bench", "--log-dir", "/results/R1", "--suite", "x",
                                                "--tests", "layout"])
        self.assertEqual(node_cmd(cfg, 1, v)[-1], "x")
        self.assertIsNone(node_cmd({}, 0, v))


if __name__ == "__main__":
    unittest.main()
