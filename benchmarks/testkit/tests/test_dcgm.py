import json
import os
import tempfile
import unittest

from testkit.parsers import dcgm


def _diag(memory="Pass", warn=None):
    res = {"entity_group": "GPU", "entity_group_id": 1, "entity_id": 2, "status": memory}
    if warn:
        res["warnings"] = [{"error_category": 3, "error_id": 95, "error_severity": 2, "warning": warn}]
    return json.dumps({
        "DCGM Diagnostic": {"test_categories": [
            {"category": "Deployment", "tests": [
                {"name": "software", "results": [{"entity_group": "GPU", "entity_group_id": 1, "entity_id": i,
                                                   "status": "Pass"} for i in range(4)],
                 "test_summary": {"status": "Pass"}}]},
            {"category": "Hardware", "tests": [
                {"name": "memory", "results": [res], "test_summary": {"status": memory}},
                {"name": "pulse_test", "results": [], "test_summary": {"status": "Skip"}}]}],
            "version": "4.7.1"},
        "entity_groups": [], "metadata": {"version": "4.7.1"}})


DIAG_PASS = _diag()
DIAG_FAIL = _diag("Fail", "A DBE (double bit error) occurred on GPU 2.")
DIAG_ERROR = json.dumps({"DCGM Diagnostic": {"runtime_error": "Error: No available testing entities.\n",
                                             "version": "4.7.1"}})

GPU = """GPU 0000000{n}:01:00.0
    Product Name                          : NVIDIA GB300
    Fabric
        State                             : {state}
        Status                            : Success
        CliqueId                          : 32766
        Health
            Bandwidth                     : Full
            State                         : Ignored-nested
    ECC Errors
        Volatile
            SRAM Correctable              : 0
            SRAM Uncorrectable Parity     : 0
            SRAM Uncorrectable SEC-DED    : 0
            DRAM Correctable              : 3
            DRAM Uncorrectable            : {unc}
        Aggregate
            DRAM Uncorrectable            : 7
    Remapped Rows
        Correctable Error                 : 0
        Uncorrectable Error               : {unc}
        Pending                           : No
        Remapping Failure Occurred        : No
        Bank Remap Availability Histogram
            Max                           : 2048 bank(s)
"""


def smi(bad_gpu=None, fabric="Completed", count=4):
    head = "==============NVSMI LOG==============\n\nDriver Version : 580.95.05\nAttached GPUs : 4\n"
    return head + "".join(GPU.format(n=n, unc=1 if n == bad_gpu else 0, state=fabric) for n in range(count))


SMI = smi()


def nvlink(down=None):
    out = []
    for g in range(4):
        out.append("GPU {}: NVIDIA GB300 (UUID: GPU-{})".format(g, g))
        for l in range(18):
            out.append("\t Link {}: {}".format(l, "<inactive>" if (g, l) == down else "50 GB/s"))
    return "\n".join(out) + "\n"


NVLINK = nvlink()
HEALTHY = json.dumps({"body": {"Overall Health": {"value": "Healthy"}}, "header": ["Health Monitor Report"]})


class Host:
    gpus = 4


class DcgmParseTest(unittest.TestCase):

    def run_parse(self, files, cfg=None):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "raw"))
            for name, text in files.items():
                with open(os.path.join(d, "raw", name), "w") as f:
                    f.write(text)
            return dcgm.parse(d, cfg or {"level": 3}, Host())

    def test_all_good(self):
        out = self.run_parse({"diag.json": DIAG_PASS, "nvidia-smi-q.txt": SMI, "nvlink.txt": NVLINK,
                              "health.json": "Health monitor systems set\n" + HEALTHY})
        self.assertEqual(out["status"], "pass", out["reason"])
        c = out["checks"]
        self.assertEqual(c["diag:software"]["result"], "pass")
        self.assertEqual(c["diag:pulse_test"]["result"], "skip")
        for name in ("gpu_count", "ecc", "remapped_rows", "fabric_state", "nvlink", "health"):
            self.assertEqual(c[name]["result"], "pass", name)
        self.assertEqual(out["metrics"]["nvlink_up_min"]["value"], 18)
        self.assertEqual(out["metrics"]["ecc_uncorrectable"]["value"], 0)

    def test_failures(self):
        out = self.run_parse({"diag.json": DIAG_FAIL, "nvidia-smi-q.txt": smi(bad_gpu=2, fabric="In Progress"),
                              "nvlink.txt": nvlink(down=(3, 5)),
                              "health.json": json.dumps({"body": {"Overall Health": {"value": "Failure"}}})})
        self.assertEqual(out["status"], "fail")
        c = out["checks"]
        self.assertIn("gpu2: A DBE", c["diag:memory"]["reason"])
        self.assertIn("00000002:01:00.0 1", c["ecc"]["reason"])
        self.assertEqual(c["remapped_rows"]["result"], "fail")
        self.assertIn("In Progress", c["fabric_state"]["reason"])
        self.assertEqual(c["nvlink"]["reason"], "links up: GPU3 17/18")
        self.assertEqual(c["health"]["result"], "fail")
        self.assertEqual(out["metrics"]["ecc_uncorrectable"]["value"], 1)

    def test_runtime_error_and_missing_output(self):
        out = self.run_parse({"diag.json": DIAG_ERROR, "nvidia-smi-q.txt": smi(count=3)})
        self.assertEqual(out["checks"]["diag"]["reason"], "Error: No available testing entities.")
        two = json.dumps({"DCGM Diagnostic": {"runtime_error": "Error: Unable to complete.\nError: No entities.\n"}})
        self.assertEqual(self.run_parse({"diag.json": two})["checks"]["diag"]["reason"],
                         "Error: Unable to complete. / Error: No entities.")
        self.assertEqual(out["checks"]["gpu_count"]["reason"], "3 GPUs (expected 4)")
        self.assertEqual(out["checks"]["nvlink"]["result"], "skip")
        out = self.run_parse({})
        self.assertEqual(out["checks"]["diag"]["reason"], "no JSON output from dcgmi diag")
        self.assertEqual(out["status"], "fail")

    def test_checks_can_be_disabled(self):
        out = self.run_parse({"diag.json": DIAG_PASS, "nvidia-smi-q.txt": smi(bad_gpu=1)},
                             {"checks": ["nvlink"], "health": False})
        self.assertNotIn("ecc", out["checks"])
        self.assertNotIn("health", out["checks"])
        self.assertEqual(out["status"], "pass")


if __name__ == "__main__":
    unittest.main()
