"""Command line entry point for testkit."""

import argparse
import os
import sys

from testkit import __version__

def build_parser():
    parser = argparse.ArgumentParser(
        prog="testkit",
        description="Run GB300 acceptance tests on many nodes over SSH and build an HTML report.",
    )
    parser.add_argument("--version", action="version", version="testkit " + __version__)
    parser.add_argument("-c", "--cluster", default="cluster.yaml",
                        help="cluster file: ssh, network and hosts (default: %(default)s)")
    parser.add_argument("-t", "--tests", default="tests.yaml",
                        help="tests file: which tests to run and how (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = sub.add_parser("init", help="write commented cluster.yaml and tests.yaml templates")
    init.add_argument("--dir", default=".", help="where to write the files (default: current directory)")
    init.add_argument("--racks", type=int, default=1, help="example racks in cluster.yaml (default: 1)")
    init.add_argument("--trays", type=int, default=2, help="example trays per rack (default: 2)")
    init.add_argument("--force", action="store_true", help="overwrite existing files")

    imp = sub.add_parser("import", help="read the IP plan (.xlsx or .csv) into the hosts of cluster.yaml (-c)")
    imp.add_argument("plan", help="IP plan file, e.g. gb300-nic-ip-plan.xlsx")
    imp.add_argument("--sheet", help="sheet name in .xlsx (default: hosts, else the first sheet)")
    imp.add_argument("--dry-run", action="store_true", help="check and summarize without writing cluster.yaml")

    val = sub.add_parser("validate", help="check cluster.yaml and tests.yaml")
    val.add_argument("--ssh", action="store_true", help="also check that every host is reachable over SSH")

    plan = sub.add_parser("plan", help="print groups, ranks and the rendered command for every node")
    _add_selection(plan)
    plan.add_argument("-v", "--verbose", action="store_true", help="print env and command of every node")

    sub.add_parser("doctor", help="check local dependencies (python, PyYAML, ssh, rsync, sb)")

    nic = sub.add_parser("nic", help="S3: discover NICs and compare with the golden host")
    nic.add_argument("action", choices=["discover", "show"])
    nic.add_argument("host", nargs="?", help="host for 'show' (default: the golden host)")
    nic.add_argument("--hosts", help="hosts for 'discover' (default: all)")
    nic.add_argument("--exclude", help="hosts to leave out")

    net = sub.add_parser("net", help="S4: configure mgmt, storage, IPoIB and LOM policy routing with netplan, then verify")
    net.add_argument("action", choices=["apply", "verify"])
    net.add_argument("--hosts", help="hosts to configure (default: all)")
    net.add_argument("--exclude", help="hosts to leave out")
    net.add_argument("--dry-run", action="store_true", help="show the planned changes and netplan diff only")
    net.add_argument("--yes", action="store_true", help="allow reboots when ib_mode.reboot is ask")

    run = sub.add_parser("run", help="S5-S8: run enabled test items in the built-in order")
    _add_selection(run)
    run.add_argument("--from", dest="start_from", metavar="ITEM",
                     help="start at this item (or prefix) and run the enabled items after it, e.g. after a gate stop")
    run.add_argument("--force", action="store_true",
                     help="also run hosts that failed an earlier item (ignore stop_on_failure and full_rack)")
    run.add_argument("--no-gate", action="store_true",
                     help="do not stop the run when a gated item fails (tests.yaml gate)")

    st = sub.add_parser("status", help="show per-item and per-host results")
    st.add_argument("--host", help="show every item of one host")
    logs = sub.add_parser("logs", help="show a run, or the logs of one host in a run")
    logs.add_argument("run_id")
    logs.add_argument("--host")
    logs.add_argument("--item", help="only this item")
    logs.add_argument("--follow", action="store_true", help="stream live container logs over ssh")
    cancel = sub.add_parser("cancel", help="stop all containers of a run")
    cancel.add_argument("run_id")
    rep = sub.add_parser("report", help="S9: build the HTML report (and CSV) from local results")
    rep.add_argument("--out", help="output directory (default: <results_dir>/report)")
    rep.add_argument("--title", help="report title (default: report.title in tests.yaml)")
    rep.add_argument("--archive", action="store_true", help="also pack all run directories into raw-logs.tar.gz")
    return parser


def _add_selection(p):
    p.add_argument("items", nargs="*", help="only these items or prefixes, for example moe or moe.rack")
    p.add_argument("--hosts", help="narrow the host selection, for example R1, gb300-r1-t01 or tag:batch1")
    p.add_argument("--exclude", help="hosts to leave out, same syntax as --hosts")


def _print_problems(problems):
    for msg in problems.warnings:
        print("warning: " + msg)
    for msg in problems.errors:
        print("error: " + msg)


def cmd_init(args):
    from testkit import scaffold
    if args.racks < 1 or args.trays < 1:
        print("error: --racks and --trays must be at least 1")
        return 2
    os.makedirs(args.dir, exist_ok=True)
    files = {"cluster.yaml": scaffold.cluster_text(args.racks, args.trays), "tests.yaml": scaffold.tests_text()}
    exists = [n for n in files if os.path.exists(os.path.join(args.dir, n))]
    if exists and not args.force:
        print("error: {} already exists in {}; use --force to overwrite".format(", ".join(exists), args.dir))
        return 1
    for name, text in files.items():
        with open(os.path.join(args.dir, name), "w", encoding="utf-8") as f:
            f.write(text)
        print("wrote " + os.path.join(args.dir, name))
    print("edit the host list and addresses, then run: testkit validate")
    return 0


def _load(args):
    from testkit.checks import check_tests
    from testkit.config import load_both
    cluster, tests, problems = load_both(args.cluster, args.tests)
    check_tests(tests, cluster, problems)
    return cluster, tests, problems


def cmd_validate(args):
    from testkit.checks import enabled_items
    cluster, tests, problems = _load(args)
    racks = ", ".join("{} {}".format(r, len(cluster.rack_hosts(r))) for r in cluster.racks)
    golden = [h.name for h in cluster.hosts if h.golden]
    print("{}: {} hosts ({}), golden {}".format(args.cluster, len(cluster.hosts), racks or "-",
                                                 golden[0] if golden else "-"))
    items = [item.id for item, _ in enabled_items(tests)]
    print("{}: {} items enabled: {}".format(args.tests, len(items), ", ".join(items) or "-"))
    if args.ssh and cluster.hosts:
        from testkit.ssh import check_reachable
        result = check_reachable(cluster.ssh, cluster.hosts)
        bad = {n: m for n, (ok, m) in result.items() if not ok}
        print("ssh: {}/{} hosts reachable".format(len(result) - len(bad), len(result)))
        for name, msg in sorted(bad.items()):
            problems.error("{}: ssh failed: {}".format(name, msg))
    _print_problems(problems)
    print("result: {} error(s), {} warning(s)".format(len(problems.errors), len(problems.warnings)))
    return 0 if problems.ok else 1


def cmd_plan(args):
    from testkit.items import known_selector
    from testkit.plan import build_plan, format_plan
    from testkit.selection import SelectionError
    cluster, tests, problems = _load(args)
    for s in args.items:
        if not known_selector(s):
            problems.error("'{}' is not a test item or prefix".format(s))
    if not problems.ok:
        _print_problems(problems)
        return 1
    try:
        plans = build_plan(cluster, tests, args.items, only=args.hosts, exclude=args.exclude)
    except SelectionError as e:
        print("error: " + str(e))
        return 1
    if not plans:
        print("nothing to run: no enabled item matches")
        return 0
    sys.stdout.write(format_plan(plans, tests.runtime["master_port_base"], verbose=args.verbose))
    for msg in problems.warnings:
        print("warning: " + msg)
    return 0


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def cmd_run(args):
    import signal
    from testkit.docker import DockerOps
    from testkit.executor import run_items
    from testkit.items import known_selector
    from testkit.selection import SelectionError
    # Stop containers on Ctrl-C and on kill (TERM), also when started in the background.
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    cluster, tests, problems = _load(args)
    for s in args.items:
        if not known_selector(s):
            problems.error("'{}' is not a test item or prefix".format(s))
    if args.start_from and not known_selector(args.start_from):
        problems.error("--from '{}' is not a test item or prefix".format(args.start_from))
    if not problems.ok:
        _print_problems(problems)
        return 1
    for msg in problems.warnings:
        print("warning: " + msg)
    try:
        _, code = run_items(cluster, tests, args.items, args.hosts, args.exclude, DockerOps(cluster.ssh),
                            force=args.force, out=lambda m: print(m, flush=True), start_from=args.start_from,
                            gate=not args.no_gate)
    except SelectionError as e:
        print("error: " + str(e))
        return 1
    return code


def _hosts(cluster, args):
    from testkit.selection import resolve
    hosts = resolve(cluster, args.hosts or "all")
    if args.exclude:
        drop = {h.name for h in resolve(cluster, args.exclude)}
        hosts = [h for h in hosts if h.name not in drop]
    return hosts


def cmd_nicnet(args, cluster, tests):
    from testkit.selection import SelectionError
    if args.command == "nic" and args.action == "show":
        from testkit import nic
        name = args.host or next((h.name for h in cluster.hosts if h.golden), None)
        if not name:
            print("error: no host given and no golden host in cluster.yaml")
            return 1
        return nic.show(cluster, tests, name)
    try:
        hosts = _hosts(cluster, args)
    except SelectionError as e:
        print("error: " + str(e))
        return 1
    if not hosts:
        print("no hosts selected")
        return 1
    if args.command == "nic":
        from testkit import nic
        _, statuses = nic.discover(cluster, tests, hosts, out=lambda m: print(m, flush=True))
        bad = sum(1 for s in statuses.values() if s != "pass")
        print("{} hosts, {} with problems; details: testkit nic show <host>".format(len(statuses), bad))
        return 0 if not bad else 1
    from testkit import net
    if args.action == "verify":
        return net.verify_only(cluster, tests, hosts, out=lambda m: print(m, flush=True))
    return net.NetRun(cluster, tests, hosts, out=lambda m: print(m, flush=True), dry_run=args.dry_run,
                      assume_yes=args.yes).run()


def _load_quiet(args):
    from testkit.config import load_both
    cluster, tests, _ = load_both(args.cluster, args.tests)
    return cluster, tests


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "init":
        return cmd_init(args)
    if args.command == "import":
        from testkit.plan_import import run_import
        return run_import(args.plan, args.cluster, sheet=args.sheet, dry_run=args.dry_run)
    if args.command == "doctor":
        from testkit import doctor
        return doctor.run()
    from testkit.util import ConfigError
    try:
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "plan":
            return cmd_plan(args)
        if args.command == "run":
            return cmd_run(args)
        from testkit import runs
        cluster, tests = _load_quiet(args)
        if args.command == "status":
            return runs.status(tests, cluster, host=args.host)
        if args.command == "logs":
            from testkit.docker import DockerOps
            ops = {"cluster": cluster, "docker": DockerOps(cluster.ssh)} if args.follow else None
            return runs.logs(tests, args.run_id, host=args.host, item=args.item, follow=args.follow, ops=ops)
        if args.command in ("nic", "net"):
            return cmd_nicnet(args, cluster, tests)
        if args.command == "report":
            from testkit.report import write_report
            data, written = write_report(cluster, tests, out_dir=args.out, title=args.title, archive=args.archive)
            n_fail = sum(1 for h in data["hosts"] if h["overall"] == "fail")
            print("{} hosts, {} items, {} hosts with own failures".format(len(data["hosts"]), len(data["items"]), n_fail))
            for path in written:
                print("wrote " + path)
            return 0
        if args.command == "cancel":
            from testkit.docker import DockerOps
            return runs.cancel(tests, cluster, args.run_id, DockerOps(cluster.ssh))
    except ConfigError as e:
        print("error: " + str(e))
        return 1
    return 2
