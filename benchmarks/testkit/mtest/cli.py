"""Command line entry point for mtest."""

import argparse
import sys

from mtest import __version__

# Each subcommand is filled in by a later implementation phase; until then it
# reports which phase will provide it.
PENDING = {
    "init": "P1",
    "validate": "P1",
    "plan": "P1",
    "doctor": "P1",
    "nic": "P3",
    "net": "P3",
    "run": "P2",
    "status": "P2",
    "logs": "P2",
    "cancel": "P2",
    "report": "P6",
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mtest",
        description="Run GB300 acceptance tests on many nodes over SSH and build an HTML report.",
    )
    parser.add_argument("--version", action="version", version="mtest " + __version__)
    parser.add_argument("-c", "--cluster", default="cluster.yaml",
                        help="cluster file: ssh, network and hosts (default: %(default)s)")
    parser.add_argument("-t", "--tests", default="tests.yaml",
                        help="tests file: which tests to run and how (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser("init", help="write commented cluster.yaml and tests.yaml templates")
    sub.add_parser("validate", help="check cluster.yaml and tests.yaml, and SSH reachability")
    sub.add_parser("plan", help="print groups, ranks and the rendered command for every node")
    sub.add_parser("doctor", help="check local dependencies (python, PyYAML, ssh, rsync, sb)")

    nic = sub.add_parser("nic", help="S3: discover NICs and compare with the golden host")
    nic.add_argument("action", choices=["discover", "golden"])
    nic.add_argument("host", nargs="?", help="host name for 'golden'")

    net = sub.add_parser("net", help="S4: apply network configuration")
    net.add_argument("action", choices=["apply"])
    net.add_argument("--dry-run", action="store_true", help="show the changes without applying them")

    run = sub.add_parser("run", help="S5-S8: run enabled test items in the built-in order")
    run.add_argument("items", nargs="*", help="only run these items, for example moe.rack")
    run.add_argument("--hosts", help="narrow the host selection, for example R1 or tag:batch1")
    run.add_argument("--exclude", help="comma-separated hosts to leave out")

    sub.add_parser("status", help="show per-item and per-host progress")
    logs = sub.add_parser("logs", help="show logs of a run")
    logs.add_argument("run_id")
    logs.add_argument("--host")
    cancel = sub.add_parser("cancel", help="stop all containers of a run")
    cancel.add_argument("run_id")
    sub.add_parser("report", help="S9: build the HTML report from local results")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    print("mtest {}: not implemented yet (planned in {})".format(args.command, PENDING[args.command]),
          file=sys.stderr)
    return 2
