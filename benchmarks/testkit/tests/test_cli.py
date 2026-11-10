import contextlib
import io
import unittest

from mtest import __version__
from mtest.cli import PENDING, build_parser, main


class CliTest(unittest.TestCase):

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(__version__, out.getvalue())

    def test_no_command_prints_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("usage: mtest", out.getvalue())

    def test_every_subcommand_parses(self):
        parser = build_parser()
        samples = {
            "nic": ["nic", "discover"],
            "net": ["net", "apply", "--dry-run"],
            "run": ["run", "moe.rack", "--hosts", "R1"],
            "logs": ["logs", "20261112-0930-dcgm"],
            "cancel": ["cancel", "20261112-0930-dcgm"],
        }
        for name in PENDING:
            args = parser.parse_args(samples.get(name, [name]))
            self.assertEqual(args.command, name)

    def test_pending_command_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["status"]), 2)
        self.assertIn("not implemented yet", err.getvalue())


if __name__ == "__main__":
    unittest.main()
