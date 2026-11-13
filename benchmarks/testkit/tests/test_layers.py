import json
import os
import unittest

from testkit.config import load_both
from testkit.executor import run_items
from testkit.outlier import outliers
from testkit.parsers import links, nvlink, nvshmem, superbench
from testkit.state import StateStore, read_json
from tests.helpers import Workspace
from tests.test_executor import FakeOps

H1, H2, H3 = "gb300-r1-t01", "gb300-r1-t02", "gb300-r1-t03"

NVLINK_S = "GPU 0: NVIDIA GB300 (UUID: GPU-a)\n" + "".join("\t Link {}: 50 GB/s\n".format(i) for i in range(18))
NVLINK_S_DOWN = NVLINK_S.replace("Link 7: 50 GB/s", "Link 7: <inactive>")
NVLINK_E_INLINE = """GPU 0: NVIDIA GB300 (UUID: GPU-a)
\t Link 0: Replay Errors: 0
\t Link 0: Recovery Errors: 0
\t Link 0: CRC Errors: {crc}
\t Link 1: Replay Errors: 0
"""
NVLINK_E_BLOCK = """GPU 0: NVIDIA GB300 (UUID: GPU-a)
\t Link 0:
\t\t Malformed Packet Errors: 0
\t\t Effective Errors: {eff}
\t\t Effective BER: 15e-255
\t\t Symbol Errors: {sym}
\t\t FEC Errors - 0: {fec}
GPU 1: NVIDIA GB300 (UUID: GPU-b)
\t Link 3:
\t\t Effective Errors: 0
"""
P2P = """ \tGPU0\tGPU1\tGPU2\tGPU3\t
 GPU0\tX\tOK\tOK\tOK\t
 GPU1\tOK\tX\tOK\tOK\t
 GPU2\tOK\tOK\tX\t{v}\t
 GPU3\tOK\tOK\tOK\tX\t

Legend:
  X    = Self
  OK   = Status Ok
"""
GPUS = "0, 00000008:01:00.0, 1\n1, 00000009:01:00.0, 2\n2, 00000018:01:00.0, 3\n3, 00000019:01:00.0, 4\n"


def ib(state="4: ACTIVE", rate="800 Gb/sec (2X XDR)", sym=0, n=4):
    return {"mlx5_{}/1".format(i): {"link_layer": "InfiniBand", "state": state if i == 0 else "4: ACTIVE",
                                    "rate": rate, "counters": {"symbol_error": sym if i == 0 else 0,
                                                               "port_xmit_wait": 5 + sym}} for i in range(n)}


def snap(status=NVLINK_S, errors=NVLINK_E_INLINE.format(crc=0), p2p="OK", ports=None):
    status4 = "".join(status.replace("GPU 0", "GPU {}".format(g)) for g in range(4))
    return {"smi": {"gpus": GPUS, "nvlink_status": status4, "nvlink_errors": errors, "p2p": P2P.format(v=p2p)},
            "rc": {"gpus": 0}, "ib": ports if ports is not None else ib()}


class Host:
    gpus = 4


class LinksParserTest(unittest.TestCase):

    def test_nvlink_error_layouts(self):
        inline = links.parse_nvlink_errors(NVLINK_E_INLINE.format(crc=3))
        self.assertEqual(inline[0][0], {"Replay Errors": 0, "Recovery Errors": 0, "CRC Errors": 3})
        block = links.parse_nvlink_errors(NVLINK_E_BLOCK.format(eff=1, sym=9, fec=100))
        self.assertEqual(block[0][0]["Effective Errors"], 1)
        self.assertNotIn("Effective BER", block[0][0])
        self.assertEqual(block[1][3], {"Effective Errors": 0})

    def test_delta_ignores_fec_and_symbol(self):
        a = snap(errors=NVLINK_E_BLOCK.format(eff=0, sym=1, fec=10))
        b = snap(errors=NVLINK_E_BLOCK.format(eff=2, sym=50, fec=900), ports=ib(sym=3))
        d = links.delta(a, b)
        self.assertEqual([k for k, _, _ in d["increased"]], ["GPU0 link 0: Effective Errors", "mlx5_0/1: symbol_error"])
        self.assertIn("mlx5_0/1: port_xmit_wait", [k for k, _, _ in d["ignored"]])
        checks, metrics, problems = links.delta_checks(d, {"mlx5_0": "rail 0"})
        self.assertEqual(checks["link_errors"]["result"], "fail")
        self.assertIn("mlx5_0/1: symbol_error (rail 0) +3", checks["link_errors"]["reason"])
        self.assertEqual(metrics["link_error_increase"]["value"], 5)
        d = links.delta(a, a)
        self.assertEqual(d["increased"], [])

    def test_delta_link_down_and_reset(self):
        a = snap(errors=NVLINK_E_INLINE.format(crc=5))
        b = snap(status=NVLINK_S_DOWN, errors=NVLINK_E_INLINE.format(crc=0), ports=ib(state="1: DOWN"))
        d = links.delta(a, b)
        self.assertEqual(d["reset"], [("GPU0 link 0: CRC Errors", 5, 0)])
        self.assertIn("GPU0 link 7: 50 GB/s -> <inactive>", d["state"])
        self.assertIn("mlx5_0/1: 4: ACTIVE -> 1: DOWN", d["state"])

    def test_state_checks(self):
        ok = links.state_checks(snap(), {"ib_rate_gbps": 800}, Host())
        self.assertEqual(ok["status"], "pass", ok["reason"])
        self.assertEqual(ok["checks"]["nvlink_up"]["reason"], "18 links up on every GPU")
        bad = links.state_checks(snap(status=NVLINK_S_DOWN, p2p="NS", ports=ib(state="1: DOWN", rate="400 Gb/sec")),
                                 {"ib_rate_gbps": 800}, Host(), {"rails": [{"rail": 0, "ibdev": "mlx5_0"}]})
        self.assertEqual(bad["status"], "fail")
        self.assertIn("GPU0 17/18 up (down: 7)", bad["checks"]["nvlink_up"]["reason"])
        self.assertIn("GPU2->GPU3 NS", bad["checks"]["p2p"]["reason"])
        self.assertIn("mlx5_0/1 (rail 0) 1: DOWN", bad["checks"]["ib_active"]["reason"])
        self.assertEqual(bad["checks"]["ib_rate"]["result"], "fail")
        slow = snap(status=NVLINK_S.replace("Link 3: 50 GB/s", "Link 3: 25 GB/s"))
        self.assertIn("GPU0 link 3 25.0 GB/s", links.state_checks(slow, {}, Host())["checks"]["nvlink_speed"]["reason"])
        nogpu = {"smi": {"gpus": "nvidia-smi: not found"}, "rc": {"gpus": 127}, "ib": {}}
        self.assertEqual(links.state_checks(nogpu, {}, Host())["checks"]["gpu_count"]["result"], "fail")


def nvbandwidth_text(hosts, low_rank=None, gpus=4):
    n = len(hosts) * gpus
    lines = ["rank {} ({}), device {}: fabricUuid=x, cliques={{}}, state=Completed".format(
        r, hosts[r // gpus], r % gpus) for r in range(n)]
    lines += ["", "Running multinode_device_to_device_memcpy_read_ce.",
              "memcpy CE GPU(row) <- GPU(column) bandwidth (GB/s)",
              "  " + "".join("{:>10}".format(c) for c in range(n))]
    for r in range(n):
        vals = ["N/A" if c == r else "{:.2f}".format(500.0 if low_rank in (r, c) else 780.0 + (r + c) % 3)
                for c in range(n)]
        lines.append("{:>2}".format(r) + "".join("{:>10}".format(v) for v in vals))
    lines += ["", "SUM multinode_device_to_device_memcpy_read_ce 99999.00", ""]
    return "\n".join(lines)


def nvloom_text(hosts, low=None, gpus=4):
    n = len(hosts) * gpus
    lines = ["nvloom_cli 2.0.0"]
    lines += ["Process {:03d} ({}): device {}: NVIDIA GB300; rackGuid: abc".format(p, hosts[p // gpus], p % gpus)
              for p in range(n)]
    lines += ["", "Running pairwise_device_to_device_write_sm", "\t" + "\t".join(str(c) for c in range(n)) + "\t"]
    for r in range(n):
        lines.append(str(r) + "\t" + "\t".join("N/A" if c == r else ("300.00" if low == r else "700.00")
                                              for c in range(n)) + "\t")
    lines += ["", "Min pairwise_device_to_device_write_sm 300.00 GB/s", "Avg pairwise_device_to_device_write_sm 690 GB/s",
              "Med pairwise_device_to_device_write_sm 700.00 GB/s", "Max pairwise_device_to_device_write_sm 700 GB/s",
              "Total pairwise_device_to_device_write_sm 1 GB/s", "", "ExecutionTime pairwise_device_to_device_write_sm 3 s",
              "Done pairwise_device_to_device_write_sm", "", "Running multicast_one_to_all_ce", "WAIVED", ""]
    return "\n".join(lines)


PAIRS_OK = """Successfully ran the multi-node diagnostic.
+---------------------------+------------------------------------------------+
| Metadata                  |                                                |
| Test Name                 | mnnvbandwidth                                  |
| Pairs Planned             | 66                                             |
| Pairs Passed              | 66                                             |
| Pairs Failed              | 0                                              |
+---------------------------+------------------------------------------------+
| Log Files                 | /var/log/nvidia-dcgm/mnnvbandwidth_pairs.out   |
+---------------------------+------------------------------------------------+
"""
PAIRS_FAIL = PAIRS_OK.replace("Successfully ran", "Failed to run").replace("Pairs Passed              | 66",
    "Pairs Passed              | 65").replace("Pairs Failed              | 0 ", "Pairs Failed              | 1 ").replace(
    "| Log Files", "| Error Summary             |                                                |\n"
    "| pair 10.20.0.12/GPU1 <-> 10.20.0.13/GPU2: cudaErrorPeerAccess                         |\n| Log Files")


def mndiag_json(status="Pass", failed_host=None):
    results = [{"host_id": i, "entity_group": "GPU", "entity_id": g, "status": "Pass"} for i in range(3) for g in range(4)]
    if failed_host is not None:
        results[failed_host * 4 + 1]["status"] = "Fail"
        results[failed_host * 4 + 1]["warnings"] = [{"warning": "transfer error", "error_id": 1}]
    return json.dumps({"DCGM Diagnostic": "DCGM Multi-Node Diagnostic", "metadata": {"num_hosts": 3},
                       "hosts": [{"host_id": i, "hostname": "10.20.0.{}".format(11 + i)} for i in range(3)],
                       "test_categories": [{"category": "Multi-Node Tests", "tests": [
                           {"name": "mnnvbandwidth", "status": status, "results": results}]}]})


NAMES = {"10.20.0.11": H1, "10.20.0.12": H2, "10.20.0.13": H3, "node-a": H1, "node-b": H2, "node-c": H3,
         H1: H1, H2: H2, H3: H3}


class NvlinkParserTest(unittest.TestCase):

    def test_pairs_text(self):
        p = nvlink.parse_pairs_text(PAIRS_OK)
        self.assertEqual((p["planned"], p["passed"], p["failed"]), (66, 66, 0))
        self.assertEqual(p["logs"], ["/var/log/nvidia-dcgm/mnnvbandwidth_pairs.out"])
        f = nvlink.parse_pairs_text(PAIRS_FAIL)
        self.assertEqual(f["failed"], 1)
        self.assertTrue(any("cudaErrorPeerAccess" in e for e in f["errors"]))

    def test_mndiag_json(self):
        j = nvlink.parse_mndiag_json(mndiag_json("Fail", failed_host=2))
        self.assertEqual(j["status"], "fail")
        self.assertEqual(j["failed"], [("10.20.0.13", "GPU1")])
        self.assertIn("10.20.0.13 GPU1: transfer error", j["messages"])
        self.assertEqual(nvlink.parse_mndiag_json("dcgmi: error")["status"], "error")
        self.assertEqual(nvlink.parse_mndiag_json(json.dumps({"runtime_error": "timeout"}))["error"], "timeout")

    def test_nvbandwidth_outlier_gpu(self):
        p = nvlink.parse_nvbandwidth(nvbandwidth_text(["node-a", "node-b", "node-c"], low_rank=5))
        self.assertEqual(p["ranks"][5], ("node-b", 1))
        t = p["tests"]["multinode_device_to_device_memcpy_read_ce"]
        self.assertEqual(len(t["rows"]), 12)
        a = nvlink.analyse_matrix("read", t, p["ranks"], NAMES, 10)
        self.assertEqual(sorted({g for _, g, _, _ in a["bad_gpus"]}), [H2 + ":GPU1"])
        self.assertEqual(a["count"], 132)
        b = nvlink.analyse_matrix("read", t, p["ranks"], NAMES, 10, baseline=600)
        self.assertEqual(len(b["below_baseline"]), 22)

    def test_nvloom(self):
        p = nvlink.parse_nvloom(nvloom_text(["node-a", "node-b", "node-c"], low=9))
        self.assertEqual(p["procs"][9], ("node-c", 1))
        self.assertTrue(p["tests"]["multicast_one_to_all_ce"]["waived"])
        t = p["tests"]["pairwise_device_to_device_write_sm"]
        self.assertEqual(t["stats"]["med"], 700.0)
        a = nvlink.analyse_matrix("pw", t, p["procs"], NAMES, 10)
        self.assertEqual([g for s, g, _, _ in a["bad_gpus"] if s == "src"], [H3 + ":GPU1"])

    def write(self, d, steps, files):
        os.makedirs(os.path.join(d, "raw"), exist_ok=True)
        with open(os.path.join(d, "done.json"), "w") as f:
            json.dump({"steps": steps}, f)
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, "raw", rel)), exist_ok=True)
            with open(os.path.join(d, "raw", rel), "w") as f:
                f.write(text)

    def test_group_pairs_failure_blames_hosts(self):
        ws = Workspace(racks=1, trays=3)
        try:
            self.write(ws.dir, [{"name": "pairs", "verdict": "fail", "reason": "1 of 66 pairs failed"},
                                {"name": "bandwidth", "verdict": "skip", "reason": "an earlier step did not pass"}],
                       {"pairs.txt": PAIRS_FAIL})
            per_host, extra = nvlink.parse_group(ws.dir, "nvlink.pairs", {}, NAMES, [H1, H2, H3], 12)
            self.assertEqual(extra["status"], "fail")
            self.assertEqual(per_host[H2]["status"], "fail")
            self.assertEqual(per_host[H3]["status"], "fail")
            self.assertNotIn("status", per_host[H1])
            self.assertEqual(extra["summary"], "GPU 对 66/65/1")
        finally:
            ws.close()

    def test_group_bandwidth_outlier(self):
        ws = Workspace(racks=1, trays=3)
        try:
            text = nvbandwidth_text(["node-a", "node-b", "node-c"], low_rank=6)
            self.write(ws.dir, [{"name": "pairs", "verdict": "pass", "reason": ""},
                                {"name": "bandwidth", "verdict": "pass", "reason": "Pass", "seconds": 9}],
                       {"pairs.txt": PAIRS_OK, "bandwidth.txt": mndiag_json(),
                        "bandwidth-logs/var/log/nvidia-dcgm/mnnvbandwidth.out": text})
            per_host, extra = nvlink.parse_group(ws.dir, "nvlink.pairs", {}, NAMES, [H1, H2, H3], 12)
            self.assertEqual(extra["status"], "fail")
            self.assertEqual(per_host[H2]["status"], "fail")
            self.assertIn(H2 + ":GPU2", per_host[H2]["reason"])
            self.assertNotIn("status", per_host[H1])
            m = per_host[H1]["metrics"]["bandwidth/multinode_device_to_device_memcpy_read_ce"]
            self.assertGreater(m["value"], 700)
            titles = [t["title"] for t in extra["tables"]]
            self.assertIn("测试用例汇总", titles)
            per_host, extra = nvlink.parse_group(ws.dir, "nvlink.pairs", {"outlier_action": "warn"}, NAMES,
                                                 [H1, H2, H3], 12)
            self.assertNotIn("status", extra)
            self.assertNotIn("status", per_host[H2])
        finally:
            ws.close()

    def test_group_without_done(self):
        ws = Workspace(racks=1, trays=1)
        try:
            _, extra = nvlink.parse_group(ws.dir, "nvlink.fabric", {}, NAMES, [H1], 4)
            self.assertEqual(extra["status"], "error")
        finally:
            ws.close()


class OtherParsersTest(unittest.TestCase):

    def test_outliers(self):
        self.assertEqual(outliers({"a": 100, "b": 101, "c": 80}, 10), {"c": (80, 100)})
        self.assertEqual(outliers({"a": 2.0, "b": 2.1, "c": 3.0}, 10, lower_better=True), {"c": (3.0, 2.1)})
        self.assertEqual(outliers({"a": 1, "b": 0.5}, 10), {})

    def test_nvshmem(self):
        ws = Workspace(racks=1, trays=1)
        try:
            raw = os.path.join(ws.dir, "raw")
            os.makedirs(raw)
            with open(os.path.join(raw, "shmem_put_bw.txt"), "w") as f:
                f.write("shmem_put_bw\n&&&& PERF shmem_put_bw___thread___size__1024___BW 10.5 +GB/sec\n"
                        "&&&& PERF shmem_put_bw___thread___size__1048576___BW 700.25 +GB/sec\n")
            with open(os.path.join(raw, "shmem_put_bw.rc"), "w") as f:
                f.write("0\n")
            with open(os.path.join(raw, "alltoall_latency.txt"), "w") as f:
                f.write("&&&& PERF alltoall_latency___warp___size__8___latency 12.5 -us\n")
            with open(os.path.join(raw, "alltoall_latency.rc"), "w") as f:
                f.write("1\n")
            _, extra = nvshmem.parse_group(ws.dir)
            rows = extra["tables"][0]["rows"]
            self.assertIn(["shmem_put_bw", "thread", "BW", "GB/sec", 1048576, 700.25], rows)
            self.assertEqual(extra["status"], "fail")
            self.assertIn("alltoall_latency exit 1", extra["reason"])
        finally:
            ws.close()

    def test_superbench_host_and_rail_outliers(self):
        per_host = {h: {"metrics": {"nvbandwidth/d2d_read_ce_sum_bw": {"value": v},
                                    "nccl-bw:bw:allreduce:nvlink/allreduce_8_busbw": {"value": 1}}}
                    for h, v in ((H1, 3000), (H2, 3010), (H3, 2000))}
        flagged = superbench.host_outliers(per_host, superbench.OUTLIER_DEFAULTS)
        self.assertEqual(list(flagged), [H3])
        many = ["h{}".format(i) for i in range(6)]
        values = {}
        for i, a in enumerate(many):
            for b in many[i + 1:]:
                values["|".join(sorted((a, b)))] = {r: (40.0 if "h5" in (a, b) and r == 2 else 95.0) for r in range(4)}
        r = {"pairs": {}, "tests": {"bw_gpu-to-gpu": {"name": "带宽", "lower_better": False, "values": values}}}
        flagged, rows = superbench.rail_outliers(r, superbench.OUTLIER_DEFAULTS)
        self.assertEqual(list(flagged), ["h5"])
        self.assertEqual(rows[0][1:3], ["h5", 2])
        tables = [{"title": "NCCL busbw（最大消息）与小消息时延", "rows": [["nccl-bw:bw:allreduce:ib", 1 << 30, 250.0]]}]
        self.assertEqual(superbench.nccl_below_baseline(tables, {"nccl-bw:bw:allreduce:ib": 300}),
                         ["nccl-bw:bw:allreduce:ib busbw 250.0 < 300 GB/s"])


FLOW = """
platform: gb300
images: {sb_worker: "img:1", sbcli: "sb:1"}
image_pull: skip
runtime: {results_dir: ./runs, run_timeout: 30, poll_interval: 0.01, rendezvous_timeout: 5}
counters: {items: %(counters)s}
smoke: {enabled: true, scope: node, hosts: all, gate: %(gate)s}
dcgm: {enabled: %(dcgm)s, scope: node, hosts: all}
links: {enabled: %(links)s, scope: node, hosts: all, ib_rate_gbps: 800}
superbench:
  nvl_domain: {enabled: %(sharp)s, scope: rack, hosts: all, config: config_gb300_nvl_domain.yaml}
"""


class FlowTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def load(self, gate="false", dcgm="false", links="false", sharp="false", counters="null", extra=""):
        self.ws.write("tests.yaml", FLOW % {"gate": gate, "dcgm": dcgm, "links": links, "sharp": sharp,
                                            "counters": counters} + extra)
        cluster, tests, problems = load_both(self.ws.network, self.ws.tests)
        self.assertEqual(problems.errors, [])
        return cluster, tests

    def run_(self, cluster, tests, ops, **kw):
        return run_items(cluster, tests, kw.pop("selectors", []), None, None, ops, out=self.lines.append, **kw)

    def record(self, tests, run_id):
        return read_json(os.path.join(tests.results_dir, run_id, "run.json"))

    def test_gate_stops_and_from_resumes(self):
        cluster, tests = self.load(gate="true", dcgm="true")
        ops = FakeOps({H2: {"exit": 1}})
        run_id, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 1)
        rec = self.record(tests, run_id)
        self.assertEqual(rec["gated_at"], {"item": "smoke", "status": "fail"})
        self.assertNotIn("dcgm", rec["items"])
        self.assertTrue(any("testkit run --from smoke" in l for l in self.lines))
        self.assertTrue(any(H2 in l and "[" in l for l in self.lines if l.startswith("  [")))
        run_id, _ = self.run_(cluster, tests, FakeOps(), start_from="dcgm")
        rec = self.record(tests, run_id)
        self.assertEqual(list(rec["items"]), ["dcgm"])
        run_id, _ = self.run_(cluster, tests, FakeOps({H2: {"exit": 1}}), gate=False)
        self.assertIn("dcgm", self.record(tests, run_id)["items"])

    def test_incomplete_rack_is_blocked(self):
        cluster, tests = self.load(sharp="true")
        ops = FakeOps({H2: {"exit": 1}})
        run_id, code = self.run_(cluster, tests, ops)
        rec = self.record(tests, run_id)
        g = rec["items"]["superbench.nvl_domain"]["groups"]["R1"]
        self.assertEqual(g["status"], "blocked")
        self.assertIn("rack R1 incomplete: 1 left out", g["reason"])
        state = StateStore(tests.results_dir).load()
        self.assertEqual(state["hosts"][H1]["superbench.nvl_domain"]["status"], "blocked")
        self.assertFalse(any(c[0] == "run" and "nvl_domain" in c[2] for c in ops.calls))
        self.assertEqual(code, 1)

    def test_counters_fail_the_host(self):
        cluster, tests = self.load(counters="[smoke]")
        before = snap()
        after = snap(errors=NVLINK_E_INLINE.format(crc=7))
        ops = FakeOps({H2: {"snaps": [before, after]}, H1: {"snaps": [before]}, H3: {"script_fails": True}})
        run_id, code = self.run_(cluster, tests, ops)
        state = StateStore(tests.results_dir).load()["hosts"]
        self.assertEqual(state[H2]["smoke"]["status"], "fail")
        self.assertIn("GPU0 link 0: CRC Errors +7", state[H2]["smoke"]["reason"])
        self.assertEqual(state[H1]["smoke"]["status"], "pass")
        self.assertEqual(state[H3]["smoke"]["status"], "pass")
        s = read_json(os.path.join(tests.results_dir, run_id, H2, "smoke", "summary.json"))
        self.assertEqual(s["checks"]["link_errors"]["result"], "fail")
        raw = read_json(os.path.join(tests.results_dir, run_id, H2, "smoke", "raw", "link-counters.json"))
        self.assertEqual(raw["delta"]["increased"], [["GPU0 link 0: CRC Errors", 0, 7]])

    def test_links_item(self):
        cluster, tests = self.load(links="true", extra="")
        ops = FakeOps({H1: {"snaps": [snap()]}, H2: {"snaps": [snap(status=NVLINK_S_DOWN)]},
                       H3: {"script_fails": True}})
        run_id, code = self.run_(cluster, tests, ops, selectors=["links"])
        state = StateStore(tests.results_dir).load()["hosts"]
        self.assertEqual(state[H1]["links"]["status"], "pass")
        self.assertEqual(state[H2]["links"]["status"], "fail")
        self.assertIn("17/18", state[H2]["links"]["reason"])
        self.assertEqual(state[H3]["links"]["status"], "error")
        self.assertEqual(code, 1)
        self.assertFalse(any(c[0] == "run" for c in ops.calls))


NVLINK_FLOW = """
platform: gb300
images: {}
image_pull: skip
runtime: {results_dir: ./runs, run_timeout: 30, poll_interval: 0.01, rendezvous_timeout: 5}
nvlink:
  pairs: {enabled: true, scope: rack, hosts: all, all_pairs: {time_to_run: 60}, bandwidth: {time_to_run: 60}}
"""


class NvlinkFlowTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.ws.write("tests.yaml", NVLINK_FLOW)
        self.cluster, self.tests, problems = load_both(self.ws.network, self.ws.tests)
        self.assertEqual(problems.errors, [])
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def test_flow(self):
        hosts = [h.name for h in self.cluster.hosts]
        done = {"steps": [{"name": "pairs", "verdict": "pass", "reason": "66 pairs passed", "seconds": 5},
                          {"name": "bandwidth", "verdict": "pass", "reason": "Pass", "seconds": 7}]}
        text = nvbandwidth_text(hosts, low_rank=1)
        files = {"done.json": json.dumps(done), "raw/pairs.txt": PAIRS_OK,
                 "raw/bandwidth.txt": mndiag_json(), "raw/bandwidth-logs/x/out.txt": text}
        ops = FakeOps({hosts[0]: {"host_out": {"done.json": (0, json.dumps(done) + "\nUNIT=inactive")},
                                  "files": files}})
        run_id, code = run_items(self.cluster, self.tests, ["nvlink"], None, None, ops, out=self.lines.append)
        rec = read_json(os.path.join(self.tests.results_dir, run_id, "run.json"))
        g = rec["items"]["nvlink.pairs"]["groups"]["R1"]
        self.assertEqual(g["status"], "fail", self.lines)
        state = StateStore(self.tests.results_dir).load()["hosts"]
        self.assertEqual(state[hosts[0]]["nvlink.pairs"]["status"], "fail")
        self.assertFalse(state[hosts[0]]["nvlink.pairs"]["group_result"])
        self.assertIn(":GPU1", state[hosts[0]]["nvlink.pairs"]["reason"])
        self.assertTrue(state[hosts[1]]["nvlink.pairs"]["group_result"])
        self.assertEqual(rec["gated_at"]["item"], "nvlink.pairs")
        cmds = [c[2] for c in ops.calls if c[0] == "host_run"]
        self.assertTrue(any("systemd-run --unit" in c and "nvlink_run.py" in c for c in cmds))
        self.assertTrue(any("90-testkit.conf" in c and "systemctl restart nvidia-dcgm" in c for c in cmds))
        self.assertTrue(any("rm -f /run/systemd/system/nvidia-dcgm.service.d/90-testkit.conf" in c for c in cmds))
        puts = {c[2].rsplit("/", 1)[-1] for c in ops.calls if c[0] == "put" and c[1] == hosts[0]}
        self.assertTrue({"args.json", "hosts.txt", "dropin.conf", "ssh.conf", "id_ed25519", "nvlink_run.py"} <= puts)
        other = {c[2].rsplit("/", 1)[-1] for c in ops.calls if c[0] == "put" and c[1] == hosts[1]}
        self.assertNotIn("id_ed25519", other)
        group = read_json(os.path.join(self.tests.results_dir, run_id, "_groups", "nvlink.pairs", "R1",
                                       "summary.json"))
        self.assertIn("GPU 对 66/66/0", group["summary"])

    def test_steps_and_args(self):
        from testkit.items import BY_ID
        from testkit.runners import nvlink_steps
        steps = nvlink_steps(BY_ID["nvlink.pairs"], {"bandwidth": {"target_num_pairs": -1, "testcases": ["a", "b"]}},
                             "/s/hosts.txt", "10.0.0.1")
        self.assertEqual(steps[0]["argv"][-1], "mnnvbandwidth.gpuPairs=all;mnnvbandwidth.time_to_run=3600")
        self.assertEqual(steps[1]["argv"][-2:], ["mnnvbandwidth.time_to_run=3600;mnnvbandwidth.targetNumPairs=-1;"
                                                 "mnnvbandwidth.testcase=a b", "--json"])
        fab = nvlink_steps(BY_ID["nvlink.fabric"], {"suites": ["pairwise", "fabric-stress"],
                                                    "time_to_run": {"default": 100, "fabric-stress": 1200},
                                                    "rich_output": ["pairwise"]}, "/h", "1.2.3.4")
        self.assertEqual([s["name"] for s in fab], ["nvloom-pairwise", "nvloom-fabric-stress"])
        self.assertIn("nvloom.suite=pairwise;nvloom.time_to_run=100;nvloom.rich_output=", fab[0]["argv"])
        self.assertEqual(fab[1]["timeout"], 1860)

    def test_cancel_stops_unit_and_timeout(self):
        hosts = [h.name for h in self.cluster.hosts]
        self.ws.write("tests.yaml", NVLINK_FLOW.replace("hosts: all,", "hosts: all, timeout: 1,"))
        cluster, tests, _ = load_both(self.ws.network, self.ws.tests)
        ops = FakeOps({hosts[0]: {"host_out": {"done.json": (0, "\nUNIT=active")}}})
        run_id, code = run_items(cluster, tests, ["nvlink"], None, None, ops, out=self.lines.append)
        rec = read_json(os.path.join(tests.results_dir, run_id, "run.json"))
        self.assertEqual(rec["items"]["nvlink.pairs"]["groups"]["R1"]["status"], "timeout")
        self.assertTrue(any(c[0] == "host_run" and c[2].startswith("systemctl stop testkit-") for c in ops.calls))


if __name__ == "__main__":
    unittest.main()


class _H:
    def __init__(self, name, rack, tray):
        self.name, self.rack, self.tray, self.gpus = name, rack, tray, 4


class _N:
    def __init__(self, name, rack, tray):
        self.host = _H(name, rack, tray)


def _nodes(racks, trays):
    return [_N("gb300-r{}-t{:02d}".format(r, t), "R{}".format(r), t) for r in range(1, racks + 1)
            for t in range(1, trays + 1)]


class IbPairsTest(unittest.TestCase):

    def names(self, rounds):
        return [[(a.host.name, b.host.name) for a, b in r] for r in rounds]

    def test_rack_adjacent(self):
        from testkit import ibpairs
        rounds, notes = ibpairs.plan(_nodes(2, 18), "rack_adjacent")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(len(rounds[0]), 18)
        self.assertEqual(self.names(rounds)[0][:2], [("gb300-r1-t01", "gb300-r1-t02"), ("gb300-r1-t03", "gb300-r1-t04")])
        self.assertEqual(notes, [])
        rounds, notes = ibpairs.plan(_nodes(1, 3), "rack_adjacent")
        self.assertEqual(self.names(rounds), [[("gb300-r1-t01", "gb300-r1-t02")], [("gb300-r1-t03", "gb300-r1-t01")]])
        self.assertIn("second round", notes[0])
        rounds, _ = ibpairs.plan(_nodes(1, 18), "rack_half")
        self.assertEqual(self.names(rounds)[0][0], ("gb300-r1-t01", "gb300-r1-t10"))

    def test_cross_rack(self):
        from testkit import ibpairs
        rounds, notes = ibpairs.plan(_nodes(2, 18), "cross_rack")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(len(rounds[0]), 18)
        self.assertEqual(self.names(rounds)[0][4], ("gb300-r1-t05", "gb300-r2-t05"))
        rounds, _ = ibpairs.plan(_nodes(4, 18), "cross_rack")
        self.assertEqual([len(r) for r in rounds], [36, 36, 36])
        racks = {frozenset((a[:8], b[:8])) for r in self.names(rounds) for a, b in r}
        self.assertEqual(len(racks), 6)
        for r in rounds:
            hosts = [n.host.name for p in r for n in p]
            self.assertEqual(len(hosts), len(set(hosts)))
        rounds, _ = ibpairs.plan(_nodes(4, 18), "cross_rack", all_rounds=False)
        self.assertEqual(len(rounds), 1)
        nodes = [n for n in _nodes(2, 3) if n.host.name != "gb300-r2-t02"]
        rounds, notes = ibpairs.plan(nodes, "cross_rack")
        self.assertEqual(len(rounds[0]), 2)
        self.assertIn("trays 2 have no peer", notes[0])
        self.assertEqual(ibpairs.plan(_nodes(1, 4), "cross_rack")[0], [])

    def test_config_uses_sorted_os_names(self):
        from testkit import ibpairs
        nodes = _nodes(1, 4)
        rounds, _ = ibpairs.plan(nodes, "rack_adjacent")
        os_name = {"gb300-r1-t01": "node-d", "gb300-r1-t02": "Node-c", "gb300-r1-t03": "node-b", "gb300-r1-t04": "a"}
        # SuperBench hostfile: a, node-b, Node-c, node-d (case-insensitive sort)
        self.assertEqual(ibpairs.config_text(rounds, os_name), "3,2;1,0\n")


SB_FLOW = """
platform: gb300
images: {sb_worker: "img:1", sbcli: "sb:1"}
image_pull: skip
runtime: {results_dir: ./runs, run_timeout: 30, poll_interval: 0.01, rendezvous_timeout: 5}
superbench:
  rack: {enabled: true, scope: rack, hosts: all, config: config_gb300_multi_node.yaml}
  cross_rack: {enabled: true, scope: group, hosts: all, config: config_gb300_multi_node.yaml}
"""


class RdmaStageTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=2, trays=2)
        self.ws.write("tests.yaml", SB_FLOW)
        self.cluster, self.tests, problems = load_both(self.ws.network, self.ws.tests)
        self.assertEqual(problems.errors, [])
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def test_pair_files_and_override(self):
        names = [h.name for h in self.cluster.hosts]
        behave = {h: {"os_name": "n{}".format(9 - i)} for i, h in enumerate(names)}
        ops = FakeOps(behave)
        written = {}
        orig = ops.put

        def put(host, path, data, mode="600"):
            if path.endswith("ib-pairs.txt"):
                written.setdefault(path, {})[host.name] = data
            return orig(host, path, data, mode)
        ops.put = put
        captured = []
        orig_run = ops.run

        def run(host, args):
            captured.append(args)
            return orig_run(host, args)
        ops.run = run
        run_items(self.cluster, self.tests, ["superbench"], None, None, ops, out=self.lines.append, gate=False)
        rack = {p: v for p, v in written.items() if "superbench.rack-R1" in p}
        self.assertEqual(len(rack), 1)
        path, per_host = next(iter(rack.items()))
        self.assertTrue(path.startswith("/mnt/testkit/"))
        self.assertEqual(sorted(per_host), names[:2])
        # R1 hosts are n9 (t01) and n8 (t02); sorted hostfile of the group: n8, n9
        self.assertEqual(per_host[names[0]], "1,0\n")
        cross = next(v for p, v in written.items() if "superbench.cross_rack" in p)
        self.assertEqual(sorted(cross), names)
        # group hostfile n6 n7 n8 n9 = r2-t02, r2-t01, r1-t02, r1-t01; pairs t01-t01, t02-t02
        self.assertEqual(cross[names[0]], "3,1;2,0\n")
        env = [a for args in captured for a in args if a.startswith("TESTKIT_SB_OVERRIDES=")]
        self.assertTrue(env and "ib-traffic:perftest:multinode-lat.parameters.config=/mnt/testkit/" in env[0])
        posts = [c[2] for c in ops.calls if c[0] == "host_run" and "rm -rf /mnt/testkit/" in c[2]]
        self.assertTrue(posts)

    def test_os_hostnames_map_to_hosts(self):
        mapping = {"node-x": H1, "node-y": H2}
        values = {"node-x_node-y": 1}
        metrics = {"ib-traffic:perftest:multinode/ib_write_bw_8388608_gpu-to-gpu_0_0:node-x_node-y:{}".format(r): 90.0
                   for r in range(4)}
        r = superbench.rdma(metrics, mapping)
        self.assertEqual(list(r["pairs"]), ["{}|{}".format(H1, H2)])
        self.assertTrue(values)


class KernelMlxlinkTest(unittest.TestCase):

    def test_kernel_delta(self):
        a = snap()
        b = dict(snap(), kernel={"error": "", "lines": [
            "2026-11-11T09:00:00 host kernel: NVRM: Xid (PCI:0008:01:00): 79, pid=1, GPU has fallen off the bus.",
            "2026-11-11T09:00:01 host kernel: mlx5_core 0000:03:00.0: mlx5_cmd_out_err: info"]})
        d = links.delta(a, b)
        self.assertEqual(len(d["kernel"]), 1)
        checks, _, problems = links.delta_checks(d)
        self.assertEqual(checks["kernel_log"]["result"], "fail")
        self.assertIn("Xid", problems[-1])
        d = links.delta(a, b, {"kernel_ignore": [r"Xid \([^)]*\): 79,"]})
        self.assertEqual(d["kernel"], [])

    def test_counter_actions(self):
        from testkit.executor import counters_cfg
        from testkit.items import BY_ID

        class T:
            data = {}
        self.assertEqual(counters_cfg(T, BY_ID["superbench.rack"])["action"], "fail")
        self.assertEqual(counters_cfg(T, BY_ID["superbench.nvl_domain"])["action"], "warn")
        self.assertEqual(counters_cfg(T, BY_ID["moe.rack"])["action"], "warn")
        self.assertIsNone(counters_cfg(T, BY_ID["links"]))
        T.data = {"counters": {"items": ["moe.rack"]}}
        self.assertEqual(counters_cfg(T, BY_ID["moe.rack"])["action"], "fail")
        self.assertIsNone(counters_cfg(T, BY_ID["dcgm"]))

    def test_mlxlink(self):
        doc = {"result": {"output": {
            "Operational Info": {"State": {"values": ["Active"]}},
            "Physical Counters and BER Info": {"Effective Physical Errors": {"values": ["3"]},
                                               "Effective Physical BER": {"values": ["15E-255"]},
                                               "Raw Physical BER": {"values": ["1.2E-07"]}},
            "Module Info": {"Temperature [C]": {"values": ["48"]}, "Vendor Name": {"values": ["NVIDIA"]}}}}}
        info = links.parse_mlxlink(doc)
        self.assertEqual(info["effective_errors"], "3")
        self.assertEqual(info["temperature"], "48")
        s = dict(snap(), mlxlink={"mlx5_0/1": doc, "mlx5_1/1": {"error": "mlxlink: not found"}})
        out = links.state_checks(s, {}, Host(), {"rails": [{"rail": 0, "ibdev": "mlx5_0"}]})
        self.assertEqual(out["checks"]["ib_cable"]["result"], "fail")
        self.assertIn("mlx5_0/1 (rail 0) effective physical errors 3", out["checks"]["ib_cable"]["reason"])
        self.assertEqual(out["metrics"]["ib_raw_ber_max"]["value"], 1.2e-07)
        none = dict(snap(), mlxlink={"mlx5_0/1": {"error": "mlxlink: not found"}})
        self.assertEqual(links.state_checks(none, {}, Host())["checks"]["ib_cable"]["result"], "skip")
        boot = dict(snap(), kernel={"error": "", "lines": ["x NVRM: Xid (PCI:1): 48, DBE"]})
        self.assertEqual(links.state_checks(boot, {}, Host())["checks"]["kernel_log"]["result"], "warn")
        self.assertEqual(links.state_checks(boot, {"max_kernel_errors": 0}, Host())["status"], "fail")


NCCL_LOG = """node-a:100:100 [0] NCCL INFO NET/IB : Using [0]mlx5_0:1/IB [1]mlx5_1:1/IB [2]mlx5_2:1/IB [3]mlx5_3:1/IB{extra} ; OOB eth0:10.0.0.1<0>
node-a:100:120 [0] NCCL INFO Channel 00/0 : 0[0] -> 4[0] [send] via NET/IB/0/GDRDMA
node-a:101:121 [1] NCCL INFO NET/IB : Using [0]mlx5_0:1/IB [1]mlx5_1:1/IB [2]mlx5_2:1/IB [3]mlx5_3:1/IB{extra} ; OOB eth0
node-a:101:121 [1] NCCL INFO Channel 01/0 : 1[1] -> 5[1] [send] via NET/IB/{nic1}/GDRDMA
node-a:101:121 [1] NCCL INFO Channel 02/0 : 1[1] -> 6[2] [send] via NET/IB/2(2)/GDRDMA
node-b:200:200 [0] NCCL INFO Channel 00/0 : 4[0] -> 0[0] [receive] via NET/IB/0/GDRDMA
"""


class NcclLogTest(unittest.TestCase):

    def test_check(self):
        from testkit.parsers import nccl_log
        rails = {H1: {0: "mlx5_0", 1: "mlx5_1", 2: "mlx5_2", 3: "mlx5_3"}}
        mapping = {"node-a": H1, "node-b": H2}
        ok = NCCL_LOG.format(extra="", nic1=1).splitlines()
        problems, rows, seen = nccl_log.check(ok, mapping, rails)
        self.assertTrue(seen)
        self.assertEqual(problems, {})
        self.assertIn((H1, 1, "mlx5_1", "mlx5_1", "pass"), rows)
        self.assertIn((H1, 1, "mlx5_2", "mlx5_1", "PXN"), rows)
        bad = NCCL_LOG.format(extra=" [4]mlx5_4:1/RoCE", nic1=3).splitlines()
        bad.append("node-b:200:201 [0] NCCL INFO NET/Socket : Using [0]eth0:10.0.0.2<0>")
        problems, _, _ = nccl_log.check(bad, mapping, rails)
        self.assertIn("NCCL uses mlx5_4 (RoCE), not an IB rail", problems[H1])
        self.assertIn("GPU1 sends on mlx5_3, its rail is mlx5_1", problems[H1])
        self.assertEqual(problems[H2], ["NCCL fell back to NET/Socket (TCP)"])
        self.assertEqual(nccl_log.check(["random line"], mapping, rails), ({}, [], False))
        self.assertEqual(nccl_log.rails_by_gpu({"rails": [[0, 0, "mlx5_0", 0], {"rail": 1, "gpu": 1, "ibdev": "mlx5_1"}]}),
                         {0: "mlx5_0", 1: "mlx5_1"})

    def test_parse_group_hca_and_small_latency(self):
        ws = Workspace(racks=1, trays=2)
        try:
            run = os.path.join(ws.dir, "sb", "run")
            os.makedirs(run)
            d = {"node": "node-a", "nccl-bw:bw:allreduce:mnnvl-ib/return_code": 0}
            for size, t, bw in ((8, 20.0, 0.1), (65536, 35.5, 2.0), (8589934592, 9000.0, 300.0)):
                d["nccl-bw:bw:allreduce:mnnvl-ib/allreduce_{}_time".format(size)] = t
                d["nccl-bw:bw:allreduce:mnnvl-ib/allreduce_{}_busbw".format(size)] = bw
            with open(os.path.join(run, "results-summary.jsonl"), "w") as f:
                f.write(json.dumps(d) + "\n" + json.dumps({"node": "node-b"}) + "\n")
            rdir = os.path.join(run, "nodes", "node-a", "benchmarks", "nccl-bw:bw:allreduce:mnnvl-ib", "rank0")
            os.makedirs(rdir)
            with open(os.path.join(rdir, "results.json"), "w") as f:
                json.dump({"raw_data": {"raw_output_0": [NCCL_LOG.format(extra=" [4]mlx5_4:1/RoCE", nic1=1)]}}, f)
            mapping = {"node-a": H1, "node-b": H2}
            rails = {H1: {0: "mlx5_0", 1: "mlx5_1", 2: "mlx5_2", 3: "mlx5_3"}}
            per_host, extra = superbench.parse_group(ws.dir, mapping, False, {}, rails=rails)
            self.assertEqual(extra["status"], "fail")
            self.assertEqual(per_host[H1]["checks"]["nccl_hca"]["result"], "fail")
            self.assertEqual(per_host[H1]["status"], "fail")
            row = extra["tables"][0]["rows"][0]
            self.assertEqual(row, ["nccl-bw:bw:allreduce:mnnvl-ib", 8589934592, 300.0, 20.0, 35.5])
            self.assertEqual(extra["tables"][0]["head"][3:], ["8 B 时延 us", "65536 B 时延 us"])
            self.assertEqual(extra["tables"][1]["title"], "NCCL 网卡使用（GPU → 网卡，期望为本 rail）")
            per_host, extra = superbench.parse_group(ws.dir, mapping, False, {"hca_check": "warn"}, rails=rails)
            self.assertNotIn("status", extra)
            self.assertEqual(per_host[H1]["checks"]["nccl_hca"]["result"], "warn")
            per_host, _ = superbench.parse_group(ws.dir, mapping, True, {}, rails=rails)
            m = per_host[H1]["metrics"]["nccl-bw:bw:allreduce:mnnvl-ib/time_8B"]
            self.assertEqual((m["value"], m["unit"]), (20.0, "us"))
        finally:
            ws.close()
