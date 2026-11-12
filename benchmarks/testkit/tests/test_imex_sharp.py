import json
import os
import tempfile
import unittest

from mtest.config import load_both
from mtest.executor import run_items
from mtest.parsers import imex, sharp
from mtest.state import StateStore, read_json
from tests.helpers import Workspace
from tests.test_executor import FakeOps

HOSTS = ["gb300-r1-t01", "gb300-r1-t02", "gb300-r1-t03"]
IPS = ["10.20.0.11", "10.20.0.12", "10.20.0.13"]


def ctl(states=("READY", "READY", "READY"), cell="C", domain="UP"):
    lines = ["Connectivity Table Legend:", "I - Invalid", ""]
    lines += ["Node #{} - {} - {} - Version: 580.95.05".format(i, ip, st) for i, (ip, st) in enumerate(zip(IPS, states))]
    lines += ["", " Nodes From\\To  0   1   2"]
    for i in range(3):
        row = ["C"] * 3
        if i == 2:
            row[1] = cell
        lines.append("       {}        {}".format(i, "   ".join(row)))
    lines += ["Domain State: {}".format(domain)]
    return "\n".join(lines) + "\n"


CHECK_OK = """  [L0] PASS  nvidia-imex installed  580
================ IMEX check summary ================
L0  PASS  nvidia-imex installed               nvidia-imex 580.95.05
L1  PASS  imex channel0 device                /dev/nvidia-caps-imex-channels/channel0
L1  PASS  local daemon state                  READY
L2  FAIL  connectivity matrix all C           non-C cells: N=1
L3  WARN  GPU fabric probe Complete           no GPU details (IMEX_CMD_ALLOW_SENSITIVE_INFO=0)
L3  SKIP  nvidia-imex-ctl -sanity             not supported by this version
PASS=3 FAIL=1 WARN=1 SKIP=1
RESULT: FAIL
"""
CHECK_BAD = CHECK_OK.replace("L1  PASS  local daemon state                  READY",
                             "L1  FAIL  local daemon state                  UNAVAILABLE")


def write(d, name, text):
    os.makedirs(os.path.join(d, "raw"), exist_ok=True)
    with open(os.path.join(d, "raw", name), "w") as f:
        f.write(text)


class ImexParseTest(unittest.TestCase):

    def test_check_and_ctl(self):
        rows = imex.parse_check(CHECK_OK)
        self.assertEqual(rows[0], ("L0", "PASS", "nvidia-imex installed", "nvidia-imex 580.95.05"))
        self.assertEqual(len(rows), 6)
        c = imex.parse_ctl(ctl(states=("READY", "READY", "UNAVAILABLE"), cell="N", domain="DEGRADED"))
        self.assertEqual(c["nodes"][2], (2, "10.20.0.13", "UNAVAILABLE"))
        self.assertEqual(c["matrix"][2], ["C", "N", "C"])
        self.assertEqual(c["domain"], "DEGRADED")

    def test_group(self):
        tmp = tempfile.mkdtemp()
        dirs = {h: os.path.join(tmp, h) for h in HOSTS}
        for h in HOSTS:
            write(dirs[h], "imex-check.txt", CHECK_BAD if h == HOSTS[2] else CHECK_OK)
            write(dirs[h], "imex-configure.txt", "configured\nIMEX-CONFIGURE-OK\n")
        m = dirs[HOSTS[0]]
        write(m, "group-pre.txt", ctl())
        write(m, "t1-build.rc", "0")
        write(m, "t1-pair.rc", "0")
        write(m, "t1-pair.log", "rank 1: import OK\nwrite: 712.4 GB/s\nPASS\n")
        write(m, "t1-all.rc", "1")
        write(m, "t1-all.log", "slice 1 PASS\nslice 2 FAIL\n")
        write(m, "t2-skip.txt", "/opt/superbench/bin/nvbandwidth has no multinode_* tests\n")
        per_host, extra = imex.parse_group(m, dirs, True, 3)
        self.assertEqual(per_host[HOSTS[0]]["status"], "pass")          # L2 FAIL is domain-wide, not this host
        self.assertEqual(per_host[HOSTS[0]]["checks"]["L2 connectivity matrix all C"]["result"], "fail")
        self.assertEqual(per_host[HOSTS[0]]["checks"]["L3 GPU fabric probe Complete"]["result"], "pass")
        self.assertEqual(per_host[HOSTS[2]]["status"], "fail")
        self.assertIn("local daemon state", per_host[HOSTS[2]]["reason"])
        rows = {r[0]: r for r in extra["tables"][0]["rows"]}
        self.assertEqual(rows["Domain State"][1:], ["pass", "UP"])
        self.assertEqual(rows["T1 fabric IPC（2 节点）"][1:], ["pass", "write 712.4 GB/s"])
        self.assertEqual(rows["T1 fabric IPC（全部节点）"][1], "fail")
        self.assertEqual(rows["T2 nvbandwidth multinode"][1], "skip")
        self.assertEqual(extra["status"], "fail")
        self.assertEqual(len(extra["matrix"]["cells"]), 3)

    def test_l3_failures(self):
        tmp = tempfile.mkdtemp()
        dirs = {h: os.path.join(tmp, h) for h in HOSTS}
        l3 = ("L3  FAIL  nvidia-smi Fabric State             2 Completed 2 In Progress\n"
              "L3  FAIL  Fabric Health Healthy               1 GPU(s) degraded\n")
        for h in HOSTS:
            write(dirs[h], "imex-check.txt", CHECK_OK + (l3 if h == HOSTS[0] else ""))
        write(dirs[HOSTS[0]], "group-pre.txt", ctl())
        per_host, extra = imex.parse_group(dirs[HOSTS[0]], dirs, False, 3)
        self.assertEqual(per_host[HOSTS[0]]["status"], "fail")
        self.assertIn("nvidia-smi Fabric State", per_host[HOSTS[0]]["reason"])
        self.assertNotIn("Fabric Health", per_host[HOSTS[0]]["reason"])
        self.assertEqual(per_host[HOSTS[1]]["status"], "pass")
        self.assertIn("L3 Fabric Health Healthy: 1 GPU(s) degraded", extra["reason"])
        self.assertNotIn("connectivity matrix", extra["reason"])     # L2 rows come from nvidia-imex-ctl -N

    def test_domain_down(self):
        tmp = tempfile.mkdtemp()
        dirs = {h: os.path.join(tmp, h) for h in HOSTS}
        for h in HOSTS:
            write(dirs[h], "imex-check.txt", CHECK_OK)
        write(dirs[HOSTS[0]], "group-pre.txt", ctl(domain="DOWN") + "IMEX domain not UP after 300s\n")
        _, extra = imex.parse_group(dirs[HOSTS[0]], dirs, False, 3)
        self.assertIn("Domain State: DOWN", extra["reason"])
        self.assertNotIn("imex_configure", _[HOSTS[0]]["checks"])


def sharp_jsonl(cases, fail=None, ops=("allreduce",)):
    d = {"node": IPS[0]}
    for op in ops:
        for i, c in enumerate(cases):
            name = "nccl-bw:sharp:{}:{}".format(op, c)
            d[name + "/return_code"] = 1 if (op, c) == fail else 0
            for size in (1048576, 8589934592):
                d["{}/{}_{}_busbw".format(name, op, size)] = (100.0 + 50 * i) * (1 if size < 2 ** 30 else 8)
    return json.dumps(d) + "\n" + json.dumps({"node": IPS[1]}) + "\n"


class SharpParseTest(unittest.TestCase):

    def run_dir(self, text, log=None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "sb", "run"))
        with open(os.path.join(d, "sb", "run", "results-summary.jsonl"), "w") as f:
            f.write(text)
        if log:
            p = os.path.join(d, "sb", "run", "nodes", IPS[0], "benchmarks", "nccl-bw:sharp:allreduce:O-B", "rank0")
            os.makedirs(p)
            with open(os.path.join(p, "results.json"), "w") as f:
                json.dump({"raw_data": {"raw_output_0": [log]}}, f)
        return d

    def test_scale_out(self):
        log = ("gb300-r1-t01:123:456 [0] NCCL INFO NVLS multicast support is available on dev 0\n"
               "gb300-r1-t01:123:456 [0] NCCL INFO Channel 00/0 : 0[0] -> 1[1] via P2P\n"
               "gb300-r1-t01:123:456 [0] NCCL INFO AllReduce: 8589934592 Bytes -> Algo NVLS proto SIMPLE\n")
        d = self.run_dir(sharp_jsonl(["O-A", "O-B"]), log)
        mapping = dict(zip(IPS, HOSTS))
        _, extra = sharp.parse_group(d, mapping)
        self.assertNotIn("status", extra)
        self.assertEqual(extra["tables"][1]["rows"], [["allreduce", "O-B / O-A", 1.5]])
        self.assertEqual([s["name"] for s in extra["charts"][0]["series"]], ["O-A", "O-B"])
        self.assertIn("NVLS multicast support is available", extra["log_tail"])
        self.assertIn("Algo NVLS", extra["log_tail"])
        self.assertNotIn("via P2P", extra["log_tail"])

    def test_failed_case(self):
        d = self.run_dir(sharp_jsonl(["O-A", "O-B", "O-C", "O-D"], fail=("allreduce", "O-C")))
        _, extra = sharp.parse_group(d, dict(zip(IPS, HOSTS)))
        self.assertEqual((extra["status"], extra["reason"]), ("fail", "failed: allreduce O-C"))
        rows = {r[1]: r for r in extra["tables"][0]["rows"]}
        self.assertEqual(rows["O-C"][2], "fail")
        self.assertIn(["allreduce", "O-D / O-C", 1.25], extra["tables"][1]["rows"])
        self.assertIn("未找到 NCCL 日志", extra["log_tail"])


TESTS = """
images: {sb_worker: "worker:1", sbcli: "sbcli:1"}
image_pull: skip
runtime: {results_dir: ./runs, poll_interval: 0.01}
imex: {enabled: true, scope: rack, hosts: all, wait_ready_seconds: 30}
sharp:
  common: {config: config_gb300_sharp.yaml, ops: [allreduce]}
  scale_out: {enabled: true, scope: rack, hosts: all, cases: [O-A, O-B]}
"""


class FlowTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.ws.write("tests.yaml", TESTS)
        self.cluster, self.tests, problems = load_both(self.ws.cluster, self.ws.tests)
        self.assertEqual(problems.errors, [])
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def run_(self, sel, behave):
        ops = FakeOps(behave)
        run_id, code = run_items(self.cluster, self.tests, sel, None, None, ops, out=self.lines.append)
        return ops, run_id, code

    def test_imex_flow(self):
        out = {"nvidia-imex-ctl -N": (0, ctl()), "imex-check.sh": (0, CHECK_OK),
               "echo IMEX-CONFIGURE-OK": (0, "configured\nIMEX-CONFIGURE-OK\n")}
        behave = {h: {"polls": 10 ** 6, "host_out": out} for h in HOSTS}
        ops, run_id, code = self.run_(["imex"], behave)
        calls = [c for c in ops.calls if c[0] == "host_run"]
        conf = [c for c in calls if "imex-configure.sh --nodes" in c[2]]
        self.assertEqual(sorted(c[1] for c in conf), HOSTS)
        self.assertIn("--expect 3", conf[0][2])
        lists = [c for c in ops.calls if c[0] == "put" and c[2].endswith("/nodes.list")]
        self.assertEqual(len({c[2] for c in lists}), 1)             # same path on every host
        self.assertEqual([c[1] for c in calls if "nvidia-imex-ctl -N" in c[2]], [HOSTS[0]])
        launcher = [c for c in ops.calls if c[0] == "exec"]
        self.assertEqual(launcher[0][2], "sh /mtest/imex_bench.sh")
        tools = sorted(os.path.basename(c[2]) for c in ops.calls if c[0] == "put" and ".mtest/" in c[2])
        self.assertEqual(tools, ["imex_bench.sh", "imex_bench.sh", "imex_bench.sh", "imex_fabric_ipc_test.c",
                                 "imex_fabric_ipc_test.c", "imex_fabric_ipc_test.c"])
        g = read_json(os.path.join(self.tests.results_dir, run_id, "_groups", "imex", "R1", "summary.json"))
        self.assertEqual(g["matrix"]["cells"][0], ["C", "C", "C"])
        st = StateStore(self.tests.results_dir).load()["hosts"]
        self.assertEqual(st[HOSTS[1]]["imex"]["status"], "pass", self.lines)
        self.assertTrue(st[HOSTS[1]]["imex"]["group_result"])

    def test_imex_group_failure_reaches_every_host(self):
        out = {"nvidia-imex-ctl -N": (0, ctl()), "imex-check.sh": (0, CHECK_OK),
               "echo IMEX-CONFIGURE-OK": (0, "IMEX-CONFIGURE-OK\n")}
        behave = {h: {"polls": 10 ** 6, "host_out": out, "files": {"raw/t1-build.rc": "1\n"}} for h in HOSTS}
        ops, run_id, code = self.run_(["imex"], behave)
        st = StateStore(self.tests.results_dir).load()["hosts"]
        self.assertEqual([st[h]["imex"]["status"] for h in HOSTS], ["fail"] * 3)
        self.assertTrue(all(st[h]["imex"]["group_result"] for h in HOSTS))

    def test_imex_domain_never_up(self):
        out = {"nvidia-imex-ctl -N": (1, ctl(domain="DOWN") + "IMEX domain not UP after 30s\n"),
               "imex-check.sh": (0, CHECK_OK), "echo IMEX-CONFIGURE-OK": (0, "IMEX-CONFIGURE-OK\n")}
        behave = {h: {"host_out": out} for h in HOSTS}
        ops, run_id, code = self.run_(["imex"], behave)
        self.assertEqual(code, 1)
        self.assertEqual([c for c in ops.calls if c[0] == "run"], [])
        st = StateStore(self.tests.results_dir).load()["hosts"]
        self.assertEqual(st[HOSTS[0]]["imex"]["status"], "error")
        self.assertIn("IMEX domain not UP", st[HOSTS[0]]["imex"]["reason"])
        self.assertTrue(st[HOSTS[0]]["imex"]["group_result"])
        self.assertEqual(st[HOSTS[1]]["imex"]["status"], "error")
        self.assertTrue(os.path.isfile(os.path.join(self.tests.results_dir, run_id, HOSTS[2], "imex", "raw",
                                                    "imex-check.txt")))

    def test_sharp_flow(self):
        behave = {HOSTS[0]: {"files": {"sb/run/results-summary.jsonl": sharp_jsonl(["O-A", "O-B"])}}}
        ops, run_id, code = self.run_(["sharp"], behave)
        self.assertEqual(code, 0, self.lines)
        run = [c for c in ops.calls if c[0] == "run"]
        self.assertEqual([c[1] for c in run], [HOSTS[0]])
        g = read_json(os.path.join(self.tests.results_dir, run_id, "_groups", "sharp.scale_out", "R1", "summary.json"))
        self.assertEqual(g["tables"][1]["rows"], [["allreduce", "O-B / O-A", 1.5]])


if __name__ == "__main__":
    unittest.main()
