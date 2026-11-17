import csv
import io
import json
import os
import re
import unittest

from testkit.items import ITEMS
from testkit.report import build_data, hosts_csv, metrics_csv, overall, render_html, write_report
from testkit.state import StateStore, write_json
from tests.report_fixture import RUN, make_results


class ReportTest(unittest.TestCase):

    def setUp(self):
        self.ws, self.cluster, self.tests = make_results()
        self.data = build_data(self.cluster, self.tests)
        self.hosts = {h["name"]: h for h in self.data["hosts"]}

    def tearDown(self):
        self.ws.close()

    def test_items_and_columns(self):
        items = {i["id"]: i for i in self.data["items"]}
        self.assertEqual(list(items), ["dcgm", "moe.rack"])
        self.assertEqual(items["dcgm"]["kind"], "node")
        self.assertEqual(items["dcgm"]["layer"], "L1")
        self.assertEqual(items["moe.rack"]["kind"], "group")
        cols = {c["id"]: c for c in items["dcgm"]["cols"]}
        self.assertEqual(cols["gemm_fp8"]["type"], "num")
        self.assertEqual(cols["gemm_fp8"]["unit"], "TFLOPS")
        self.assertTrue(cols["latency"]["lower_better"])
        self.assertEqual(cols["driver"]["type"], "text")
        self.assertEqual(cols["check:ecc"]["type"], "check")

    def test_layers_and_rdma_summary_in_l2(self):
        res = self.tests.results_dir
        store = StateStore(res)
        pair = ["gb300-r1-t01", "gb300-r1-t02"]
        rdma = {"pairs": {"|".join(pair): 0},
                "tests": {"bw": {"id": "bw", "name": "bw", "unit": "GB/s", "cmd": "", "lower_better": False,
                                 "values": {"|".join(pair): {"0": 390.0}}}}}
        # The L4 soak result comes after the L2 one, as in a full run.
        for item, extra in (("superbench.rack", {"rdma": rdma}), ("superbench.soak", {})):
            group = dict({"item": item, "group": "R1", "mode": "delegate", "status": "pass", "run_id": RUN,
                          "hosts": pair}, **extra)
            write_json(os.path.join(res, RUN, "_groups", item, "R1", "summary.json"), group)
            store.update({h: {item: {"status": "pass", "run_id": RUN, "group": "R1", "rank": r, "group_result": True}}
                          for r, h in enumerate(pair)}, (item, "R1", group))
        items = build_data(self.cluster, self.tests)["items"]
        self.assertEqual([(i["id"], i["layer"]) for i in items],
                         [("dcgm", "L1"), ("superbench.rack", "L2"), ("rdma", "L2"), ("superbench.soak", "L4"),
                          ("moe.rack", "L5")])
        self.assertEqual(items[2]["title"], "RDMA 打流汇总（ib-traffic：柜内、跨柜）")
        self.assertFalse([i.title for i in ITEMS if re.match(r"[SL]\d", i.title)])

    def test_layer_verdicts(self):
        layers = {l["id"]: l for l in self.data["layers"]}
        self.assertEqual(list(layers), ["", "L0", "L1", "L2", "L3", "L4", "L5"])
        # dcgm fails on one host; superbench.single_node is enabled but has no result.
        self.assertEqual((layers["L1"]["status"], layers["L1"]["ran"], layers["L1"]["expected"], layers["L1"]["bad"]),
                         ("fail", 1, 2, 1))
        self.assertEqual((layers["L0"]["status"], layers["L0"]["ran"]), ("none", 0))
        self.assertEqual(self.data["verdict"], {"status": "fail", "gate": None})

    def test_gate_stop_and_incomplete_layer(self):
        res = self.tests.results_dir
        later = "20261112-110000"
        write_json(os.path.join(res, later, "run.json"), {
            "run_id": later, "pid": 1, "status": "fail", "started_at": "2026-11-12T11:00:00Z",
            "finished_at": "2026-11-12T11:10:00Z", "items": {"dcgm": {"hosts": []}},
            "gated_at": {"item": "dcgm", "status": "fail"}})
        data = build_data(self.cluster, self.tests)
        self.assertEqual(data["verdict"]["gate"],
                         {"run_id": later, "item": "dcgm", "title": "DCGM 诊断", "status": "fail"})
        # A passing layer that still misses an enabled item is incomplete, not pass.
        store = StateStore(res)
        for h in data["hosts"]:
            store.update({h["name"]: {"dcgm": {"status": "pass", "run_id": RUN}}},
                         ("dcgm", h["name"], {"status": "pass", "run_id": RUN, "hosts": [h["name"]]}))
        layers = {l["id"]: l for l in build_data(self.cluster, self.tests)["layers"]}
        self.assertEqual(layers["L1"]["status"], "incomplete")

    def test_merge_results_dirs(self):
        res = self.tests.results_dir
        other = os.path.join(self.ws.dir, "other-runs")
        newer = "20261113-080000"
        host = "gb300-r1-t04"
        os.makedirs(os.path.join(other, newer, host, "dcgm"))
        write_json(os.path.join(other, newer, host, "dcgm", "summary.json"),
                   {"status": "pass", "metrics": {"gemm_fp8": {"value": 4510.0, "unit": "TFLOPS"}}})
        write_json(os.path.join(other, newer, "run.json"), {"run_id": newer, "status": "pass", "items": {}})
        # Older than the fixture run: must not replace it.
        StateStore(other).update({host: {"dcgm": {"status": "pass", "run_id": newer}},
                                  "gb300-r1-t01": {"dcgm": {"status": "fail", "run_id": "20261101-000000"}}})
        data = build_data(self.cluster, self.tests, results_dirs=[res, other])
        hosts = {h["name"]: h for h in data["hosts"]}
        self.assertEqual(hosts[host]["items"]["dcgm"]["status"], "pass")
        self.assertEqual(hosts[host]["metrics"]["dcgm"]["gemm_fp8"], 4510.0)
        self.assertEqual(hosts["gb300-r1-t01"]["items"]["dcgm"]["status"], "pass")
        self.assertEqual([r["run_id"] for r in data["runs"]], [RUN, newer])
        self.assertEqual(data["runs"][1]["path"], os.path.join(other, newer) + "/")
        self.assertEqual(data["meta"]["results_dirs"], [res, other])

    def test_host_verdicts(self):
        h3 = self.hosts["gb300-r1-t04"]
        self.assertEqual(h3["overall"], "fail")
        self.assertIn("Memory ..... Fail", h3["logs"]["dcgm"])
        self.assertEqual(h3["metrics"]["dcgm"]["check:ecc"], "fail")
        # A failed group result does not make a host fail on its own.
        self.assertEqual(self.hosts["gb300-r1-t01"]["items"]["moe.rack"]["status"], "fail")
        self.assertEqual(self.hosts["gb300-r1-t01"]["overall"], "pass")
        self.assertEqual(self.hosts["gb300-r1-t02"]["items"]["moe.rack"]["status"], "skip")
        self.assertEqual(self.hosts["gb300-r1-t03"]["versions"]["cx8_fw"], "40.44.1036")
        self.assertEqual(self.hosts["gb300-r1-t01"]["nic_map"][1]["ibdev"], "mlx5_1")
        self.assertEqual(self.hosts["gb300-r1-t01"]["items"]["dcgm"]["minutes"], 12.0)

    def test_overall_rules(self):
        self.assertEqual(overall({}), "none")
        self.assertEqual(overall({"a": {"status": "pass"}, "b": {"status": "skip"}}), "pass")
        self.assertEqual(overall({"a": {"status": "stopped", "group_result": True}}), "incomplete")
        self.assertEqual(overall({"a": {"status": "timeout"}}), "fail")
        self.assertEqual(overall({"a": {"status": "fail", "group_result": True}}), "pass")
        self.assertEqual(overall({"a": {"status": "cancelled", "group_result": True}}), "incomplete")

    def test_groups_and_runs(self):
        g = [x for x in self.data["groups"] if x["item"] == "moe.rack" and x["id"] == "R1"][0]
        self.assertEqual(g["status"], "fail")
        self.assertEqual(len(g["hosts"]), 3)
        self.assertEqual(g["charts"][0]["series"][0]["y"], [100, 200, 300])
        self.assertEqual(self.data["runs"][0]["minutes"], 60.0)
        self.assertEqual(self.data["meta"]["cluster"], "8 台 / 2 rack")

    def test_html_is_self_contained_and_escaped(self):
        html = render_html(self.data)
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("</script><b>", html)
        self.assertNotIn("http://", html.replace("http://x/", ""))
        payload = re.search(r"const D = (.*);\n", html).group(1)
        self.assertEqual(json.loads(payload.replace("<\\/", "</"))["meta"]["title"], "GB300 测试报告")

    def test_csv(self):
        rows = list(csv.reader(io.StringIO(hosts_csv(self.data))))
        self.assertEqual(rows[0][:5], ["hostname", "rack", "tray", "serial", "overall"])
        t04 = [r for r in rows if r[0] == "gb300-r1-t04"][0]
        self.assertEqual(t04[4], "fail")
        self.assertIn("dcgm: exit 1", t04[-1])
        mrows = list(csv.reader(io.StringIO(metrics_csv(self.data))))
        self.assertIn(["20261112-093000", "dcgm", "gb300-r1-t01", "gb300-r1-t01", "gemm_fp8", "4500.0", "TFLOPS"],
                      mrows)

    def test_write_report(self):
        _, written = write_report(self.cluster, self.tests, archive=True)
        names = sorted(os.path.basename(p) for p in written)
        self.assertEqual(names, ["hosts.csv", "metrics.csv", "raw-logs.tar.gz", "report.html"])
        self.tests.data["report"] = {"formats": ["html"]}
        _, written = write_report(self.cluster, self.tests, out_dir=os.path.join(self.ws.dir, "out"), title="T")
        self.assertEqual([os.path.basename(p) for p in written], ["report.html"])
        with open(written[0], encoding="utf-8") as f:
            self.assertIn("<title>T</title>", f.read())

    def test_empty_results(self):
        from tests.helpers import Workspace
        from testkit.config import load_both
        ws = Workspace(1, 2)
        try:
            cluster, tests, _ = load_both(ws.network, ws.tests)
            data = build_data(cluster, tests)
            self.assertEqual(data["items"], [])
            self.assertEqual([h["overall"] for h in data["hosts"]], ["none", "none"])
            self.assertIn("const D =", render_html(data))
        finally:
            ws.close()


if __name__ == "__main__":
    unittest.main()
