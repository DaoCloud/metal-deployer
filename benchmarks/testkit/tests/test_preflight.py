import os
import re
import unittest

from helpers import Workspace
from testkit import marks, preflight
from testkit.config import load_both
from testkit.plan import build_plan
from testkit.ssh import Result

SBCLI = "registry.local/metal-deployer/sbcli:latest"
WORKER = "superbench/main:cuda13.0"


class FakeOps:
    """Answers the preflight probe from per-host settings."""

    def __init__(self, behave=None, pull_ok=True):
        self.behave = behave or {}
        self.pull_ok = pull_ok
        self.pulled = []
        self.have = {}

    def b(self, host, key, default=None):
        return self.behave.get(host.name, {}).get(key, default)

    def host_run(self, host, command, timeout=300):
        if self.b(host, "down"):
            return Result(255, "", "ssh: connect to host port 22: Connection refused")
        lines = ["docker\t0|29.1", "runtime\tdocker runtime nvidia",
                 "gpu\t0|" + ",".join([self.b(host, "driver", "580.1")] * self.b(host, "gpus", 4)),
                 "containers\t" + " ".join(self.b(host, "busy", []))]
        for image in re.findall(r"p '?image:([^'\s]+)", command):
            if image not in self.b(host, "no_image", []) or image in self.have.get(host.name, []):
                lines.append("image:{}\tsha256:id|[\"{}@{}\"]".format(image, image.split(":")[0],
                                                                    self.b(host, "digest", "sha256:aaa")))
        for label in re.findall(r"p '?tool:([^'\s]+)", command):
            lines.append("tool:{}\t{}".format(label, "missing" if label in self.b(host, "no_tool", []) else "ok"))
        for peer in re.findall(r"p '?peer:([^'\s]+)", command):
            lines.append("peer:{}\t{}".format(peer, "fail" if peer.split(":")[0] in self.b(host, "no_peer", [])
                                              else "ok"))
        return Result(0, "\n".join(lines) + "\n")

    def pull(self, host, image, timeout=0):
        self.pulled.append((host.name, image))
        if self.pull_ok:
            self.have.setdefault(host.name, []).append(image)
            return Result(0)
        return Result(1, "", "pull denied")

    def image_info(self, host, image):
        return image in self.have.get(host.name, []), "sha256:pulled"


class PreflightTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)

    def tearDown(self):
        self.ws.close()

    def check(self, ops, items=("superbench.rack", "nvlink.pairs"), pull=False):
        cluster, tests, _ = load_both(self.ws.network, self.ws.tests)
        plans = build_plan(cluster, tests, list(items))
        out = []
        code = preflight.run(cluster, tests, cluster.hosts, plans, ops, pull=pull, out=out.append)
        needs = preflight.requirements(cluster, tests, cluster.hosts, plans)
        results = preflight.run_checks(ops, needs, tests, pull=False)
        return code, "\n".join(out), results, needs

    def test_requirements_follow_the_runner_of_each_item(self):
        _, _, _, needs = self.check(FakeOps())
        master, other = needs["gb300-r1-t01"], needs["gb300-r1-t02"]
        # delegate: sbcli only on rank 0, the SuperBench worker everywhere
        self.assertEqual(sorted(master.images), [SBCLI, WORKER])
        self.assertEqual(sorted(other.images), [WORKER])
        # nvlink: host tools on every host, rank 0 reaches the others over ssh (LOM for sb, mgmt for mpirun)
        self.assertIn("mpirun", other.tools)
        self.assertIn("systemd-run", master.tools)
        self.assertNotIn("systemd-run", other.tools)
        self.assertEqual(len(master.peers), 6)
        self.assertEqual(other.peers, {})

    def test_ready_hosts_pass(self):
        code, out, _, _ = self.check(FakeOps())
        self.assertEqual(code, 0, out)
        self.assertIn("3 hosts, 3 ready, 0 not ready, 0 warning(s)", out)

    def test_problems_fail_the_host_that_has_them(self):
        ops = FakeOps({"gb300-r1-t01": {"no_peer": ["10.10.0.13"]},
                       "gb300-r1-t02": {"gpus": 3, "busy": ["acc-x-smoke-gb300-r1-t02"]},
                       "gb300-r1-t03": {"down": True}})
        code, out, results, _ = self.check(ops)
        self.assertEqual(code, 1)
        self.assertEqual(results["gb300-r1-t01"]["peers"][0], "FAIL")
        self.assertIn("10.10.0.13:22", results["gb300-r1-t01"]["peers"][1])
        self.assertEqual(results["gb300-r1-t02"]["gpu"], ("FAIL", "3 GPUs, expected 4"))
        self.assertEqual(results["gb300-r1-t02"]["containers"][0], "FAIL")
        self.assertEqual(results["gb300-r1-t03"]["ssh"][0], "FAIL")
        self.assertEqual(results["gb300-r1-t03"]["docker"][0], "-")
        self.assertIn("3 hosts, 0 ready, 3 not ready", out)

    def test_missing_tool_names_the_item(self):
        code, _, results, _ = self.check(FakeOps({"gb300-r1-t02": {"no_tool": ["mpirun"]}}))
        self.assertEqual(code, 1)
        self.assertEqual(results["gb300-r1-t02"]["tools"], ("FAIL", "missing: mpirun (nvlink.pairs)"))

    def test_driver_and_digest_differences_warn(self):
        ops = FakeOps({"gb300-r1-t03": {"driver": "580.2", "digest": "sha256:bbb"}})
        code, out, results, _ = self.check(ops)
        self.assertEqual(code, 0, out)
        self.assertEqual(results["gb300-r1-t03"]["gpu"][0], "warn")
        self.assertIn("superbench/main:cuda13.0 digest", results["gb300-r1-t03"]["images"][1])
        self.assertEqual(results["gb300-r1-t01"]["images"][0], "ok")

    def test_missing_image_fails_unless_pulled(self):
        behave = {"gb300-r1-t02": {"no_image": [WORKER]}}
        ops = FakeOps(behave)
        code, _, results, _ = self.check(ops)
        self.assertEqual(code, 1)
        self.assertEqual(results["gb300-r1-t02"]["images"], ("FAIL", WORKER + " missing"))
        self.assertEqual(ops.pulled, [])
        ops = FakeOps(behave)
        code, out, _, _ = self.check(ops, pull=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(ops.pulled, [("gb300-r1-t02", WORKER)])
        code, out, _, _ = self.check(FakeOps(behave, pull_ok=False), pull=True)
        self.assertEqual(code, 1)
        self.assertIn("docker pull failed: pull denied", out)

    def test_table_uses_icons_and_stays_aligned(self):
        ops = FakeOps({"gb300-r1-t02": {"no_tool": ["mpirun"]}, "gb300-r1-t03": {"driver": "580.2"}})
        _, _, results, _ = self.check(ops)
        text = preflight.format_results(results, icons=True)
        rows = text.split("\n\n")[0].splitlines()
        def starts(row):
            return [marks.width(row[:m.start()]) for m in re.finditer(r"\S+", row)]
        self.assertEqual({len({tuple(starts(r)) for r in rows})}, {1}, text)
        self.assertIn("\u274c", rows[2])
        self.assertIn("gb300-r1-t02  \u274c FAIL tools: missing: mpirun", text)
        self.assertIn("\U0001f7e1 warn gpu:", text)
        plain = preflight.format_results(results, icons=False)
        self.assertNotIn("\u2705", plain)
        self.assertIn("gb300-r1-t02  FAIL tools:", plain)

    def test_icons_fall_back_to_text(self):
        class Out:
            encoding = "ascii"
        self.assertFalse(marks.fancy(Out()))
        Out.encoding = "UTF-8"
        old = os.environ.pop("TESTKIT_PLAIN", None), os.environ.pop("NO_COLOR", None)
        try:
            self.assertTrue(marks.fancy(Out()))
            os.environ["TESTKIT_PLAIN"] = "1"
            self.assertFalse(marks.fancy(Out()))
        finally:
            os.environ.pop("TESTKIT_PLAIN", None)
            for key, value in zip(("TESTKIT_PLAIN", "NO_COLOR"), old):
                if value is not None:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
