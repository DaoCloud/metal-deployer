import os
import threading
import unittest

from mtest.config import load_both
from mtest.executor import excluded_hosts, run_items
from mtest.runs import cancel, logs, status
from mtest.ssh import Result
from mtest.state import HostLocks, StateStore, read_json, write_json
from tests.helpers import Workspace

TESTS = """
images: {sb_worker: "img:1"}
image_pull: %(pull)s
runtime: {results_dir: ./runs, run_timeout: %(timeout)s, poll_interval: 0.01, rendezvous_timeout: 5}
smoke: {enabled: true, scope: %(scope)s, hosts: all, mode: %(mode)s}
dcgm: {enabled: %(dcgm)s, scope: node, hosts: all}
"""


class FakeLauncher:

    def __init__(self, rc, polls):
        self.rc = rc
        self.polls = polls
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        self.polls -= 1
        return self.rc if self.polls <= 0 else None

    def kill(self):
        self.killed = True

    def wait(self):
        return self.rc


class FakeOps:
    """In-memory stand-in for DockerOps. behave[host] tunes one host."""

    def __init__(self, behave=None, on_poll=None):
        self.behave = behave or {}
        self.containers_ = {}       # name -> {"host", "args", "polls", "rc", "status"}
        self.calls = []
        self.on_poll = on_poll
        self.lock = threading.Lock()
        self.launcher = None

    def b(self, host, key, default=None):
        return self.behave.get(host.name, {}).get(key, default)

    def ping(self, host):
        self.calls.append(("ping", host.name))
        return Result(1, "", "ssh: connect refused") if self.b(host, "down") else Result(0, "29.1")

    def mkdir(self, host, path):
        return Result(0)

    def put(self, host, path, data, mode="600"):
        self.calls.append(("put", host.name, path))
        return Result(0)

    def image_info(self, host, image):
        return (not self.b(host, "no_image"), "sha256:abc")

    def pull(self, host, image, timeout=0):
        return Result(1, "", "pull denied")

    def push(self, host, image, timeout=0):
        return Result(1, "", "no local docker")

    def containers(self, host, prefix):
        return list(self.b(host, "busy", []))

    def run(self, host, args):
        name = args[args.index("--name") + 1]
        with self.lock:
            self.containers_[name] = {"host": host.name, "args": args, "polls": self.b(host, "polls", 1),
                                      "rc": self.b(host, "exit", 0), "status": "running"}
        self.calls.append(("run", host.name, name))
        return Result(1, "", "no such image") if self.b(host, "run_fails") else Result(0, "cid")

    def states(self, host, names):
        if self.on_poll:
            self.on_poll()
        if self.b(host, "lost"):
            raise ConnectionError("ssh: timeout")
        out = {}
        with self.lock:
            for n in names:
                c = self.containers_.get(n)
                if c is None:
                    out[n] = ("missing", None)
                    continue
                if c["status"] == "running":
                    c["polls"] -= 1
                    if c["polls"] <= 0:
                        c["status"] = "exited"
                out[n] = (c["status"], c["rc"] if c["status"] == "exited" else 0)
        return out

    def exec_run(self, host, name, command, timeout):
        return Result(1, "", "peer down") if self.b(host, "peers_down") else Result(0)

    def exec_stream(self, host, name, command, env, log):
        self.calls.append(("exec", host.name, command))
        log.write(b"launcher output\n")
        self.launcher = FakeLauncher(self.b(host, "launcher_rc", 0), self.b(host, "launcher_polls", 2))
        return self.launcher

    def save_logs(self, host, name, path):
        return Result(0)

    def stop(self, host, names, grace=10):
        with self.lock:
            for n in names:
                c = self.containers_.get(n)
                if c and c["status"] == "running":
                    c["status"], c["rc"] = "exited", 137
        self.calls.append(("stop", host.name, tuple(names)))
        return Result(0)

    def remove(self, host, names):
        with self.lock:
            for n in names:
                self.containers_.pop(n, None)
        self.calls.append(("remove", host.name, tuple(names)))
        return Result(0)

    def remove_by_label(self, host, label):
        self.calls.append(("remove_by_label", host.name, label))
        return Result(0)

    def fetch(self, host, remote_dir, local_dir):
        os.makedirs(local_dir, exist_ok=True)
        summary = self.b(host, "summary")
        if summary is not None:
            write_json(os.path.join(local_dir, "summary.json"), summary)
        with open(os.path.join(local_dir, "container.log"), "w") as f:
            f.write("log of {}\n".format(host.name))
        return Result(1, "", "rsync failed") if self.b(host, "fetch_fails") else Result(0)


class ExecutorTest(unittest.TestCase):

    def setUp(self):
        self.ws = Workspace(racks=1, trays=3)
        self.lines = []

    def tearDown(self):
        self.ws.close()

    def setup(self, scope="node", mode="per-node", pull="skip", timeout=30, dcgm="false"):
        self.ws.write("tests.yaml", TESTS % {"scope": scope, "mode": mode, "pull": pull, "timeout": timeout,
                                             "dcgm": dcgm})
        cluster, tests, problems = load_both(self.ws.cluster, self.ws.tests)
        self.assertEqual(problems.errors, [])
        return cluster, tests

    def run_(self, cluster, tests, ops, selectors=None, force=False):
        return run_items(cluster, tests, selectors or [], None, None, ops, force=force, out=self.lines.append)

    def host_state(self, tests, host, item="smoke"):
        return StateStore(tests.results_dir).load()["hosts"][host][item]

    def test_node_scope_all_pass(self):
        cluster, tests = self.setup()
        ops = FakeOps({"gb300-r1-t02": {"summary": {"metrics": {"gpus": {"value": 4}}}}})
        run_id, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 0)
        run = read_json(os.path.join(tests.results_dir, run_id, "run.json"))
        self.assertEqual(run["status"], "pass")
        self.assertEqual(sorted(run["items"]["smoke"]["groups"]), ["gb300-r1-t01", "gb300-r1-t02", "gb300-r1-t03"])
        s = read_json(os.path.join(tests.results_dir, run_id, "gb300-r1-t02", "smoke", "summary.json"))
        self.assertEqual(s["status"], "pass")
        self.assertEqual(s["metrics"]["gpus"]["value"], 4)
        self.assertEqual(s["image_digest"], "sha256:abc")
        self.assertEqual(self.host_state(tests, "gb300-r1-t03")["status"], "pass")
        self.assertEqual(ops.containers_, {})
        self.assertEqual(len([c for c in ops.calls if c[0] == "run"]), 3)
        self.assertEqual([f for f in os.listdir(os.path.join(tests.results_dir, "locks")) if f.endswith(".lock")], [])

    def test_docker_args(self):
        cluster, tests = self.setup()
        captured = {}
        ops = FakeOps()
        orig = ops.run

        def run(host, args):
            captured[host.name] = args
            return orig(host, args)
        ops.run = run
        run_id, _ = self.run_(cluster, tests, ops)
        args = captured["gb300-r1-t02"]
        self.assertEqual(args[:2], ["--name", "acc-{}-smoke-gb300-r1-t02".format(run_id)])
        self.assertIn("mtest.run=" + run_id, args)
        self.assertIn("--privileged", args)
        self.assertIn("/var/lib/testkit/runs/{}/smoke/gb300-r1-t02:/results".format(run_id), args)
        self.assertIn("MTEST_NODE_RANK=0", args)
        self.assertIn("MTEST_SMOKE_EXIT=0", args)
        self.assertEqual(args[args.index("img:1") + 1:args.index("img:1") + 3], ["sh", "-c"])

    def test_rack_failure_stops_group_and_later_items(self):
        cluster, tests = self.setup(scope="rack")
        ops = FakeOps({"gb300-r1-t02": {"exit": 1, "polls": 1},
                       "gb300-r1-t01": {"polls": 50}, "gb300-r1-t03": {"polls": 50}})
        run_id, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 1)
        self.assertEqual(self.host_state(tests, "gb300-r1-t02")["status"], "fail")
        self.assertFalse(self.host_state(tests, "gb300-r1-t02")["group_result"])
        t1 = self.host_state(tests, "gb300-r1-t01")
        self.assertEqual(t1["status"], "stopped")
        self.assertIn("gb300-r1-t02 failed", t1["reason"])
        group = read_json(os.path.join(tests.results_dir, run_id, "_groups", "smoke", "R1", "summary.json"))
        self.assertEqual(group["status"], "fail")
        self.assertEqual(group["first_failed"], "gb300-r1-t02")
        self.assertEqual(excluded_hosts(StateStore(tests.results_dir).load(), "dcgm"),
                         {"gb300-r1-t02": "smoke fail in run {}".format(run_id)})

    def test_stop_on_failure_and_force(self):
        cluster, tests = self.setup(dcgm="true")
        StateStore(tests.results_dir).update({"gb300-r1-t01": {"smoke": {"status": "fail", "run_id": "old"}}})
        ops = FakeOps()
        self.run_(cluster, tests, ops, selectors=["dcgm"])
        self.assertTrue(any("gb300-r1-t01 left out (smoke fail in run old)" in l for l in self.lines))
        self.assertTrue(any("dcgm: skipped, dcgm is not implemented yet (P4)" in l for l in self.lines))
        self.assertTrue(any("finished: nothing was run" in l for l in self.lines))
        ops = FakeOps()
        self.lines = []
        self.run_(cluster, tests, ops, selectors=["smoke"])
        self.assertEqual(len([c for c in ops.calls if c[0] == "run"]), 3)

    def test_timeout(self):
        cluster, tests = self.setup(timeout=1)
        tests.runtime["poll_interval"] = 0.2
        ops = FakeOps({h.name: {"polls": 10 ** 6} for h in cluster.hosts})
        _, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 1)
        st = self.host_state(tests, "gb300-r1-t01")
        self.assertEqual(st["status"], "timeout")
        self.assertFalse(st["group_result"])
        self.assertTrue(any(c[0] == "stop" for c in ops.calls))

    def test_preflight_failures(self):
        cluster, tests = self.setup(scope="rack")
        ops = FakeOps({"gb300-r1-t03": {"no_image": True}, "gb300-r1-t01": {"busy": ["acc-other-x"]}})
        self.run_(cluster, tests, ops)
        self.assertEqual([c for c in ops.calls if c[0] == "run"], [])
        self.assertIn("image img:1 not found", self.host_state(tests, "gb300-r1-t03")["reason"])
        self.assertIn("other test containers are running", self.host_state(tests, "gb300-r1-t01")["reason"])
        self.assertEqual(self.host_state(tests, "gb300-r1-t02")["status"], "stopped")

    def test_pull_failure_and_down_host(self):
        cluster, tests = self.setup(pull="registry")
        ops = FakeOps({"gb300-r1-t01": {"no_image": True}, "gb300-r1-t02": {"down": True}})
        self.run_(cluster, tests, ops)
        self.assertIn("docker pull img:1 failed: pull denied", self.host_state(tests, "gb300-r1-t01")["reason"])
        self.assertIn("connect refused", self.host_state(tests, "gb300-r1-t02")["reason"])
        self.assertEqual(self.host_state(tests, "gb300-r1-t03")["status"], "pass")

    def test_lock_conflict(self):
        cluster, tests = self.setup()
        locks = HostLocks(tests.results_dir)
        os.makedirs(locks.dir)
        write_json(locks.path("gb300-r1-t01"), {"run_id": "other", "pid": os.getppid()})
        self.run_(cluster, tests, FakeOps())
        st = self.host_state(tests, "gb300-r1-t01")
        self.assertEqual(st["status"], "blocked")
        self.assertIn("run other", st["reason"])
        self.assertTrue(os.path.exists(locks.path("gb300-r1-t01")))

    def test_stale_lock_is_taken_over(self):
        cluster, tests = self.setup()
        locks = HostLocks(tests.results_dir)
        os.makedirs(locks.dir)
        write_json(locks.path("gb300-r1-t01"), {"run_id": "dead", "pid": 2 ** 22 + 12345})
        os.utime(locks.path("gb300-r1-t01"), (0, 0))
        self.run_(cluster, tests, FakeOps())
        self.assertEqual(self.host_state(tests, "gb300-r1-t01")["status"], "pass")

    def test_release_keeps_other_runs_locks(self):
        _, tests = self.setup()
        locks = HostLocks(tests.results_dir)
        self.assertEqual(locks.acquire(["a", "b"], "r1", "smoke"), {})
        self.assertEqual(set(locks.acquire(["b", "c"], "r2", "smoke")), {"b"})
        self.assertFalse(os.path.exists(locks.path("c")))
        write_json(locks.path("c"), {"run_id": "other", "pid": os.getppid()})
        locks.release(["a", "c"])
        self.assertFalse(os.path.exists(locks.path("a")))
        self.assertTrue(os.path.exists(locks.path("c")))
        locks.release(["c"], run_id="other")
        self.assertFalse(os.path.exists(locks.path("c")))

    def test_cancel_during_preflight_is_not_a_host_fault(self):
        cluster, tests = self.setup(pull="registry")
        ops = FakeOps({h.name: {"no_image": True} for h in cluster.hosts})
        holder = {}

        def pull(host, image, timeout=0):
            run_dir = holder.get("dir") or os.path.join(
                tests.results_dir, [d for d in os.listdir(tests.results_dir) if d[0].isdigit()][0])
            holder["dir"] = run_dir
            open(os.path.join(run_dir, "CANCEL"), "w").close()
            return Result(255, "", "killed")
        ops.pull = pull
        _, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 130)
        st = self.host_state(tests, "gb300-r1-t01")
        self.assertEqual(st["status"], "cancelled")
        self.assertTrue(st["group_result"])

    def test_lost_host(self):
        cluster, tests = self.setup()
        ops = FakeOps({"gb300-r1-t02": {"lost": True}})
        self.run_(cluster, tests, ops)
        self.assertIn("lost connection", self.host_state(tests, "gb300-r1-t02")["reason"])

    def test_summary_fail_overrides_exit_0_and_copy_error(self):
        cluster, tests = self.setup()
        ops = FakeOps({"gb300-r1-t01": {"summary": {"status": "fail"}}, "gb300-r1-t02": {"fetch_fails": True}})
        self.run_(cluster, tests, ops)
        self.assertEqual(self.host_state(tests, "gb300-r1-t01")["status"], "fail")
        self.assertFalse(self.host_state(tests, "gb300-r1-t01")["group_result"])
        st = self.host_state(tests, "gb300-r1-t02")
        self.assertEqual(st["status"], "error")
        self.assertIn("results not copied", st["reason"])

    def test_mpi_mode(self):
        cluster, tests = self.setup(scope="rack", mode="mpi")
        ops = FakeOps({h.name: {"polls": 10 ** 6} for h in cluster.hosts})
        run_id, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 0, self.lines)
        puts = [c for c in ops.calls if c[0] == "put"]
        self.assertEqual(len(puts), 3 * 5)
        execs = [c for c in ops.calls if c[0] == "exec"]
        self.assertEqual(execs[0][1], "gb300-r1-t01")
        self.assertIn("mpirun --allow-run-as-root --hostfile /mtest-ssh/hostfile --map-by node -np 3 hostname",
                      execs[0][2])
        for h in ("gb300-r1-t01", "gb300-r1-t03"):
            st = self.host_state(tests, h)
            self.assertEqual(st["status"], "pass")
            self.assertTrue(st["group_result"])
        log = os.path.join(tests.results_dir, run_id, "gb300-r1-t01", "smoke", "launcher.log")
        self.assertTrue(os.path.isfile(log))
        self.assertTrue(os.path.isfile(os.path.join(tests.results_dir, run_id, "ssh", "host_key")))

    def test_mpi_launcher_failure_is_group_level(self):
        cluster, tests = self.setup(scope="rack", mode="mpi")
        behave = {h.name: {"polls": 10 ** 6} for h in cluster.hosts}
        behave["gb300-r1-t01"]["launcher_rc"] = 1
        self.run_(cluster, tests, FakeOps(behave))
        st = self.host_state(tests, "gb300-r1-t01")
        self.assertEqual(st["status"], "fail")
        self.assertTrue(st["group_result"])
        self.assertEqual(self.host_state(tests, "gb300-r1-t02")["status"], "fail")
        self.assertEqual(excluded_hosts(StateStore(tests.results_dir).load(), "dcgm"), {})

    def test_mpi_peers_not_ready(self):
        cluster, tests = self.setup(scope="rack", mode="mpi")
        behave = {h.name: {"polls": 10 ** 6} for h in cluster.hosts}
        behave["gb300-r1-t01"]["peers_down"] = True
        _, code = self.run_(cluster, tests, FakeOps(behave))
        self.assertEqual(code, 1)
        self.assertIn("mpi peers not ready", self.host_state(tests, "gb300-r1-t01")["reason"])

    def test_delegate_starts_only_master(self):
        cluster, tests = self.setup(scope="group", mode="delegate")
        ops = FakeOps()
        _, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 0)
        self.assertEqual([c[1] for c in ops.calls if c[0] == "run"], ["gb300-r1-t01"])
        self.assertTrue(self.host_state(tests, "gb300-r1-t02")["group_result"])

    def test_cancel_file(self):
        cluster, tests = self.setup(scope="rack")
        tests.runtime["poll_interval"] = 0.05
        holder = {}

        def on_poll():
            run_dir = holder.get("dir")
            if run_dir is None:
                runs = [d for d in os.listdir(tests.results_dir) if d[0].isdigit()]
                holder["dir"] = run_dir = os.path.join(tests.results_dir, runs[0])
            open(os.path.join(run_dir, "CANCEL"), "w").close()
        ops = FakeOps({h.name: {"polls": 10 ** 6} for h in cluster.hosts}, on_poll=on_poll)
        _, code = self.run_(cluster, tests, ops)
        self.assertEqual(code, 130)
        self.assertEqual(self.host_state(tests, "gb300-r1-t02")["status"], "cancelled")

    def test_views(self):
        cluster, tests = self.setup()
        run_id, _ = self.run_(cluster, tests, FakeOps({"gb300-r1-t03": {"exit": 2}}))
        out = []
        status(tests, cluster, out=out.append)
        self.assertTrue(any(l.startswith("smoke") and " 2 " in l for l in out), out)
        self.assertTrue(any("gb300-r1-t03" in l and "error" in l for l in out))
        out = []
        logs(tests, run_id, out=out.append)
        self.assertTrue(any("[gb300-r1-t03] error" in l for l in out), out)
        out = []
        self.assertEqual(cancel(tests, cluster, run_id, FakeOps(), out=out.append), 0)
        self.assertIn("is not running", out[0])
        rec_path = os.path.join(tests.results_dir, run_id, "run.json")
        rec = read_json(rec_path)
        rec.update({"status": "running", "pid": 2 ** 22 + 12345})
        write_json(rec_path, rec)
        ops = FakeOps()
        cancel(tests, cluster, run_id, ops, out=out.append)
        self.assertEqual(len([c for c in ops.calls if c[0] == "remove_by_label"]), 3)
        self.assertEqual(read_json(rec_path)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
