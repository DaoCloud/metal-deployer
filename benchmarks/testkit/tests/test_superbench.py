import json
import os
import tempfile
import unittest

from mtest.config import load_both
from mtest.executor import run_items
from mtest.parsers import superbench as sb
from mtest.report import build_data
from mtest.state import StateStore, read_json
from tests.helpers import Workspace
from tests.test_executor import FakeOps

HOSTS = ["gb300-r1-t01", "gb300-r1-t02", "gb300-r1-t03"]
IPS = ["10.20.0.11", "10.20.0.12", "10.20.0.13"]
MAPPING = dict(zip(IPS, HOSTS))


def single_line(ip, gemm=4500.0, burn_rc=0):
    d = {"node": ip, "gpu-burn/return_code": burn_rc, "cublaslt-gemm:compute/return_code:0": 0,
         "monitor/gpu_temperature:0": 50}
    for r in range(4):
        d["cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops:{}".format(r)] = gemm - r
        d["cublaslt-gemm:compute/return_code:{}".format(r)] = 0
    for size in (1048576, 8589934592):
        d["nccl-bw:bw:allreduce:nvlink/allreduce_{}_busbw".format(size)] = 700.0 if size > 2 ** 30 else 100.0
        d["nccl-bw:bw:allreduce:nvlink/allreduce_{}_algbw".format(size)] = 400.0
    d["ib-loopback:1G/ib_write_bw_8388608:0"] = 97.0
    d["ib-loopback:1G/ib_write_bw_65536:0"] = 50.0
    return json.dumps(d)


def multi_lines(fail_rail=None):
    d = {"node": IPS[0], "ib-traffic:perftest:multinode/return_code": 0}
    pairs = [(0, 0, 1), (1, 0, 2), (2, 1, 2)]
    for rnd, a, b in pairs:
        for rail in range(4):
            v = -1 if (rnd, rail) == fail_rail else 97.5
            d["ib-traffic:perftest:multinode/ib_write_bw_8388608_gpu-to-gpu_{}_0:{}_{}:{}".format(
                rnd, IPS[a], IPS[b], rail)] = v
            d["ib-traffic:perftest:multinode-lat/ib_write_lat_2_cpu-to-cpu_{}_0:{}_{}:{}".format(
                rnd, IPS[a], IPS[b], rail)] = 2.1
    for size in (1048576, 2097152, 8589934592):
        d["nccl-bw:bw:allreduce:ib/allreduce_{}_busbw".format(size)] = size / 2 ** 27
    return json.dumps(d) + "\n" + json.dumps({"node": IPS[1]}) + "\n"


class ParserTest(unittest.TestCase):

    def write(self, text):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "sb", "run"))
        with open(os.path.join(d, "sb", "run", "results-summary.jsonl"), "w") as f:
            f.write(text)
        return d

    def test_single_node(self):
        d = self.write("\n".join([single_line(IPS[0]), single_line(IPS[1], gemm=4000.0, burn_rc=1)]) + "\n")
        per_host, extra = sb.parse_group(d, MAPPING, own=True)
        self.assertEqual(extra, {})
        a, b, c = (per_host[h] for h in HOSTS)
        self.assertEqual(a["status"], "pass")
        self.assertEqual(a["metrics"]["cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops"]["value"], 4497.0)
        self.assertEqual(a["metrics"]["cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops"]["category"], "GPU-COMPUTE")
        self.assertFalse(a["metrics"]["nccl-bw:bw:allreduce:nvlink/allreduce_8589934592_busbw"]["hidden"])
        self.assertTrue(a["metrics"]["nccl-bw:bw:allreduce:nvlink/allreduce_1048576_busbw"]["hidden"])
        self.assertTrue(a["metrics"]["nccl-bw:bw:allreduce:nvlink/allreduce_8589934592_algbw"]["hidden"])
        self.assertTrue(a["metrics"]["ib-loopback:1G/ib_write_bw_65536"]["hidden"])
        self.assertNotIn("monitor/gpu_temperature", a["metrics"])
        self.assertEqual(a["checks"]["rc:cublaslt-gemm:compute"]["result"], "pass")
        self.assertEqual((b["status"], b["reason"]), ("fail", "gpu-burn rc 1"))
        self.assertEqual(c["status"], "error")
        self.assertTrue(all(x["own"] for x in (a, b, c)))

    def test_multi_node(self):
        d = self.write(multi_lines(fail_rail=(1, 2)))
        per_host, extra = sb.parse_group(d, MAPPING, own=False)
        self.assertNotIn("status", extra)
        r = extra["rdma"]
        self.assertEqual(r["pairs"], {"gb300-r1-t01|gb300-r1-t02": 0, "gb300-r1-t01|gb300-r1-t03": 1,
                                      "gb300-r1-t02|gb300-r1-t03": 2})
        self.assertEqual(set(r["tests"]), {"bw_gpu-to-gpu", "lat_cpu-to-cpu"})
        self.assertEqual(r["tests"]["lat_cpu-to-cpu"]["unit"], "us")
        self.assertTrue(r["tests"]["lat_cpu-to-cpu"]["lower_better"])
        self.assertEqual(r["tests"]["bw_gpu-to-gpu"]["values"]["gb300-r1-t01|gb300-r1-t03"][2], -1)
        self.assertEqual(extra["tables"][0]["rows"][0], ["nccl-bw:bw:allreduce:ib", 8589934592, 64.0, "—", "—"])
        self.assertEqual(extra["charts"][0]["x"], [1048576, 2097152, 8589934592])
        self.assertNotIn("own", per_host[HOSTS[0]])

    def test_failed_benchmark_and_no_results(self):
        d = self.write(multi_lines().replace('"ib-traffic:perftest:multinode/return_code": 0',
                                             '"ib-traffic:perftest:multinode/return_code": 3'))
        _, extra = sb.parse_group(d, MAPPING, own=False)
        self.assertEqual(extra["status"], "fail")
        self.assertIn("ib-traffic:perftest:multinode rc 3", extra["reason"])
        _, extra = sb.parse_group(tempfile.mkdtemp(), MAPPING, own=False)
        self.assertEqual(extra["status"], "error")


TESTS = """
images: {sb_worker: "worker:1", sbcli: "sbcli:1"}
image_pull: skip
runtime: {results_dir: ./runs, poll_interval: 0.01}
superbench:
  common: {rule: rule_gb300.yaml}
  single_node: {enabled: %(single)s, scope: node, hosts: all, config: config_gb300_single_node.yaml,
                benchmarks: {exclude: [gpu-burn]}}
  rack: {enabled: %(rack)s, scope: rack, hosts: all, config: config_gb300_multi_node.yaml}
"""


class DelegateTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def run_(self, single, rack, behave):
        self.ws.write("tests.yaml", TESTS % {"single": single, "rack": rack})
        cluster, tests, problems = load_both(self.ws.cluster, self.ws.tests)
        self.assertEqual(problems.errors, [])
        ops = FakeOps(behave)
        run_id, code = run_items(cluster, tests, [], None, None, ops, out=self.lines.append)
        return cluster, tests, ops, run_id, code

    def test_single_node_batch(self):
        jsonl = "\n".join([single_line(IPS[0]), single_line(IPS[1], burn_rc=1), single_line(IPS[2])]) + "\n"
        behave = {HOSTS[0]: {"files": {"sb/run/results-summary.jsonl": jsonl}}}
        cluster, tests, ops, run_id, code = self.run_("true", "false", behave)
        self.assertEqual(code, 1)
        self.assertEqual([c[1] for c in ops.calls if c[0] == "run"], [HOSTS[0]])
        pre = [c for c in ops.calls if c[0] == "host_run" and "authorized_keys" in c[2] and "grep -qF" in c[2]]
        post = [c for c in ops.calls if c[0] == "host_run" and c[2].startswith("sed -i")]
        self.assertEqual(sorted(c[1] for c in pre), HOSTS)
        self.assertEqual(sorted(c[1] for c in post), HOSTS)
        self.assertIn("mtest-" + run_id, post[0][2])
        puts = sorted(os.path.basename(c[2]) for c in ops.calls if c[0] == "put")
        self.assertEqual(puts, ["id_ed25519", "inventory", "sb_run.sh"])
        state = StateStore(tests.results_dir).load()["hosts"]
        self.assertEqual(state[HOSTS[0]]["superbench.single_node"]["status"], "pass")
        self.assertEqual(state[HOSTS[1]]["superbench.single_node"]["status"], "fail")
        self.assertFalse(state[HOSTS[1]]["superbench.single_node"]["group_result"])
        self.assertEqual(state[HOSTS[2]]["superbench.single_node"]["status"], "pass")
        s = read_json(os.path.join(tests.results_dir, run_id, HOSTS[2], "superbench.single_node", "summary.json"))
        self.assertIn("cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops", s["metrics"])
        self.assertTrue(any("all" in l and "single_node" in l for l in self.lines), self.lines)

    def test_rack_group_and_report(self):
        behave = {HOSTS[0]: {"files": {"sb/run/results-summary.jsonl": multi_lines(fail_rail=(2, 1))}}}
        cluster, tests, ops, run_id, code = self.run_("false", "true", behave)
        self.assertEqual(code, 0, self.lines)
        g = read_json(os.path.join(tests.results_dir, run_id, "_groups", "superbench.rack", "R1", "summary.json"))
        self.assertIn("rdma", g)
        self.assertEqual(len(g["charts"]), 1)
        data = build_data(cluster, tests)
        r = data["rdma"]
        self.assertEqual(r["hosts"], HOSTS)
        self.assertEqual(len(r["pairs"]), 3)
        self.assertEqual([t["id"] for t in r["tests"]], ["bw_gpu-to-gpu", "lat_cpu-to-cpu"])
        k = [i for i, p in enumerate(r["pairs"]) if p[2] == 2][0]
        self.assertEqual(r["values"]["bw_gpu-to-gpu"][1][k], -1)
        self.assertEqual(r["values"]["lat_cpu-to-cpu"][3][k], 2.1)
        self.assertIn("rdma", [i["id"] for i in data["items"]])

    def test_missing_worker_image_stops_group(self):
        behave = {HOSTS[2]: {"no_image": True}}
        cluster, tests, ops, run_id, code = self.run_("false", "true", behave)
        self.assertEqual(code, 1)
        st = StateStore(tests.results_dir).load()["hosts"]
        self.assertIn("image worker:1 not found", st[HOSTS[2]]["superbench.rack"]["reason"])
        self.assertEqual([c for c in ops.calls if c[0] == "run"], [])


if __name__ == "__main__":
    unittest.main()
