import contextlib
import io
import os
import unittest

from mtest import __version__
from mtest.cli import PENDING, build_parser, main
from tests.helpers import Workspace


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class CliTest(unittest.TestCase):

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_no_command_prints_help(self):
        code, out = run([])
        self.assertEqual(code, 0)
        self.assertIn("usage: mtest", out)

    def test_every_subcommand_parses(self):
        parser = build_parser()
        samples = {
            "init": ["init", "--racks", "2"], "validate": ["validate", "--ssh"],
            "plan": ["plan", "moe", "--hosts", "R1", "-v"], "doctor": ["doctor"],
            "nic": ["nic", "discover"], "net": ["net", "apply", "--dry-run"],
            "run": ["run", "moe.rack", "--hosts", "R1", "--force"], "logs": ["logs", "x", "--host", "h"],
            "cancel": ["cancel", "x"], "status": ["status", "--host", "h"], "report": ["report", "--archive"],
        }
        for name in list(PENDING) + ["init", "validate", "plan", "doctor", "run", "status", "logs", "cancel", "report"]:
            args = parser.parse_args(samples.get(name, [name]))
            self.assertEqual(args.command, name)

    def test_pending_command_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["nic", "discover"]), 2)
        self.assertIn("not implemented yet", err.getvalue())

    def test_init_validate_plan(self):
        ws = Workspace(1, 1)
        try:
            target = os.path.join(ws.dir, "new")
            code, out = run(["init", "--dir", target, "--racks", "2", "--trays", "3"])
            self.assertEqual(code, 0)
            self.assertEqual(run(["init", "--dir", target])[0], 1)
            self.assertEqual(run(["init", "--dir", target, "--force", "--racks", "2", "--trays", "3"])[0], 0)
            c, t = os.path.join(target, "cluster.yaml"), os.path.join(target, "tests.yaml")
            code, out = run(["-c", c, "-t", t, "validate"])
            self.assertEqual(code, 0, out)
            self.assertIn("6 hosts (R1 3, R2 3), golden gb300-r1-t01", out)
            code, out = run(["-c", c, "-t", t, "plan", "moe.rack"])
            self.assertEqual(code, 0, out)
            self.assertIn("group R2: 3 hosts", out)
            self.assertEqual(run(["-c", c, "-t", t, "plan", "nope"])[0], 1)
            self.assertEqual(run(["-c", c, "-t", t, "plan", "--hosts", "R9"])[0], 1)
            self.assertEqual(run(["-c", os.path.join(target, "missing.yaml"), "-t", t, "validate"])[0], 1)
        finally:
            ws.close()

    def test_validate_reports_errors(self):
        ws = Workspace(1, 2)
        try:
            ws.edit("cluster.yaml", "ssh: 10.10.0.12", "ssh: 10.10.0.11")
            code, out = run(["-c", ws.cluster, "-t", ws.tests, "validate"])
            self.assertEqual(code, 1)
            self.assertIn("error: address 10.10.0.11 is used by", out)
        finally:
            ws.close()

    def test_doctor(self):
        code, out = run(["doctor"])
        self.assertIn("pyyaml", out)
        self.assertIn(code, (0, 1))


if __name__ == "__main__":
    unittest.main()
