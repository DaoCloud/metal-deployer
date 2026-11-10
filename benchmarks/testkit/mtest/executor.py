"""Run test items on hosts: locks, preflight, start, monitor, collect, record."""

import os
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from mtest.checks import enabled_items
from mtest.items import ITEMS, matches
from mtest.plan import build_plan
from mtest.render import render
from mtest.runners import NotReady, build_job, node_dir, peers_ready_script
from mtest.state import HostLocks, StateStore, now_iso, read_json, write_json

# Host results that keep a host out of later items (when stop_on_failure is on).
FAILED = ("fail", "error", "timeout")
# Worst first; the group status is the worst status among its hosts.
SEVERITY = ["cancelled", "timeout", "error", "fail", "blocked", "stopped", "pass"]
LOST_AFTER = 5          # consecutive failed status polls before a host counts as lost


def status_of_rc(rc):
    if rc == 0:
        return "pass"
    if rc == 1:
        return "fail"
    return "error"


def worst(statuses):
    statuses = [s for s in statuses if s]
    if not statuses:
        return "pass"
    return min(statuses, key=lambda s: SEVERITY.index(s) if s in SEVERITY else 0)


def pmap(fn, items, workers):
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        return list(pool.map(fn, items))


def run_cancellable(fn, items, workers, cancel):
    """Like pmap, but always in worker threads so Ctrl-C in the main thread sets cancel at once."""
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1)))
    futures = [pool.submit(fn, i) for i in items]
    interrupted = False
    try:
        pending = set(futures)
        while pending:
            try:
                _, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                cancel.set()
                interrupted = True
    finally:
        pool.shutdown(wait=True)
    results = [f.result() for f in futures]
    if interrupted:
        raise KeyboardInterrupt
    return results


class RunContext:

    def __init__(self, cluster, tests, run_id, ops, out=print):
        self.cluster = cluster
        self.tests = tests
        self.run_id = run_id
        self.ops = ops
        self.out = out
        self.run_dir = os.path.join(tests.results_dir, run_id)
        self.cancel = threading.Event()
        self.parallel = max(1, int(cluster.ssh.get("parallel", 24)))
        self.state = StateStore(tests.results_dir)
        self.locks = HostLocks(tests.results_dir)
        self._lock = threading.Lock()
        self._keys = None
        self.record = {"run_id": run_id, "pid": os.getpid(), "status": "running", "started_at": now_iso(),
                       "cluster": os.path.abspath(cluster.path), "tests": os.path.abspath(tests.path), "items": {}}

    def log(self, msg):
        with self._lock:
            self.out(msg)

    def cancelled(self):
        if not self.cancel.is_set() and os.path.exists(os.path.join(self.run_dir, "CANCEL")):
            self.cancel.set()
        return self.cancel.is_set()

    def save_record(self):
        with self._lock:
            write_json(os.path.join(self.run_dir, "run.json"), self.record)

    def ssh_keys(self):
        """Client key and host key for mpi-mode containers, generated once per run."""
        with self._lock:
            if self._keys is None:
                key_dir = os.path.join(self.run_dir, "ssh")
                os.makedirs(key_dir, exist_ok=True)
                for name in ("id_ed25519", "host_key"):
                    path = os.path.join(key_dir, name)
                    if not os.path.exists(path):
                        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mtest-" + self.run_id,
                                        "-f", path], check=True, stdin=subprocess.DEVNULL)

                def read(name):
                    with open(os.path.join(key_dir, name), encoding="utf-8") as f:
                        return f.read()
                self._keys = (read("id_ed25519"), read("id_ed25519.pub"), read("host_key"))
            return self._keys

    def local_dir(self, host_name, item_id):
        return os.path.join(self.run_dir, host_name, item_id)


def excluded_hosts(state, item_id):
    """{host: reason} for hosts whose own result failed in an item that runs before item_id."""
    order = [i.id for i in ITEMS]
    before = set(order[:order.index(item_id)])
    out = {}
    for host, items in state.get("hosts", {}).items():
        for other, entry in items.items():
            if other in before and entry.get("status") in FAILED and not entry.get("group_result"):
                out[host] = "{} {} in run {}".format(other, entry.get("status"), entry.get("run_id"))
                break
    return out


class GroupRunner:
    """Runs one job (one group of one item) from locks to recorded results."""

    def __init__(self, ctx, item, group, nodes, job):
        self.ctx = ctx
        self.item = item
        self.group = group
        self.nodes = nodes
        self.job = job
        self.ops = ctx.ops
        self.rt = ctx.tests.runtime
        self.hosts = {n.host.name: n for n in nodes}
        self.result = {}          # host name -> {"status", "reason", "rc"}
        self.digests = {}
        self.first_failed = None
        self.group_reason = ""
        self.host_fault = set()   # hosts whose own problem caused their result

    # ---------- helpers ----------
    def set(self, host_name, status, reason="", rc=None, fault=False):
        if host_name not in self.result:
            self.result[host_name] = {"status": status, "reason": reason, "rc": rc}
            if fault:
                self.host_fault.add(host_name)

    def by_host(self, specs):
        out = {}
        for s in specs:
            out.setdefault(s.host.name, []).append(s)
        return out

    def docker_args(self, spec):
        vars_ = self.hosts[spec.host.name].variables
        args = ["--name", spec.name, "--label", "mtest.run=" + self.ctx.run_id,
                "--label", "mtest.item=" + self.item.id, "--label", "mtest.group=" + self.group.id]
        args += [render(a, vars_) for a in self.rt["container_args"]]
        args += ["-v", "{}:/results".format(node_dir(self.ctx.tests, self.ctx.run_id, self.item.id, spec.host.name))]
        for src, dst, ro in spec.mounts:
            args += ["-v", "{}:{}{}".format(src, dst, ":ro" if ro else "")]
        for key, value in spec.env.items():
            args += ["-e", "{}={}".format(key, value)]
        args += spec.extra_args + [spec.image] + list(spec.cmd)
        return args

    # ---------- phases ----------
    def run(self):
        started = now_iso()
        t0 = time.time()
        names = list(self.hosts)
        conflicts = self.ctx.locks.acquire(names, self.ctx.run_id, self.item.id)
        if conflicts:
            who = ", ".join("{} (run {})".format(h, i.get("run_id")) for h, i in conflicts.items())
            for h in names:
                self.set(h, "blocked", "host in use: " + who)
            return self.finish(started, t0)
        try:
            if self.ctx.cancelled():
                for h in names:
                    self.set(h, "cancelled", "run cancelled before start")
            elif self.preflight():
                if self.ctx.cancelled():
                    for h in names:
                        self.set(h, "cancelled", "run cancelled before start")
                elif self.start():
                    self.monitor()
                    self.collect()
        finally:
            self.cleanup()
            self.ctx.locks.release(names)
        return self.finish(started, t0)

    def preflight(self):
        specs = self.by_host(self.job.containers)
        files = {}
        for host, path, data, mode in self.job.files:
            files.setdefault(host.name, []).append((path, data, mode))
        pull = self.ctx.tests.data.get("image_pull", "registry")

        def check(host_name):
            spec = specs[host_name][0]
            host = spec.host
            res = self.ops.ping(host)
            if not res.ok:
                return host_name, "ssh/docker not available: " + res.message()
            for image in sorted({s.image for s in specs[host_name]}):
                present, digest = self.ops.image_info(host, image)
                if not present and pull == "registry":
                    res = self.ops.pull(host, image)
                    if not res.ok:
                        return host_name, "docker pull {} failed: {}".format(image, res.message())
                elif not present and pull == "push":
                    res = self.ops.push(host, image)
                    if not res.ok:
                        return host_name, "push {} failed: {}".format(image, res.message())
                elif not present:
                    return host_name, "image {} not found (image_pull: skip)".format(image)
                if not present:
                    present, digest = self.ops.image_info(host, image)
                self.digests[host_name] = digest
            busy = self.ops.containers(host, "acc-")
            if busy is None:
                return host_name, "cannot list containers"
            if busy:
                return host_name, "other test containers are running: " + ", ".join(busy[:3])
            res = self.ops.mkdir(host, node_dir(self.ctx.tests, self.ctx.run_id, self.item.id, host_name))
            if not res.ok:
                return host_name, "cannot create result directory: " + res.message()
            for path, data, mode in files.get(host_name, []):
                res = self.ops.put(host, path, data, mode)
                if not res.ok:
                    return host_name, "cannot write {}: {}".format(path, res.message())
            return host_name, None

        bad = {h: r for h, r in pmap(check, specs, self.ctx.parallel) if r}
        if not bad:
            return True
        if self.ctx.cancelled():
            for h in self.hosts:
                self.set(h, "cancelled", "run cancelled during preflight")
            return False
        for h, reason in bad.items():
            self.set(h, "error", "preflight: " + reason, fault=True)
        first = sorted(bad)[0]
        self.group_reason = "preflight failed on {}: {}".format(first, bad[first])
        for h in self.hosts:
            self.set(h, "stopped", "group not started: " + self.group_reason)
        return False

    def start(self):
        self.started = []

        def one(spec):
            return spec, self.ops.run(spec.host, self.docker_args(spec))

        ok = True
        for spec, res in pmap(one, self.job.containers, self.ctx.parallel):
            if res.ok:
                self.started.append(spec)
            else:
                ok = False
                self.set(spec.host.name, "error", "docker run failed: " + res.message(), fault=True)
                self.first_failed = self.first_failed or spec.host.name
        if not ok and self.ctx.cancelled():
            self.result.clear()
            self.host_fault.clear()
            self.first_failed = None
            self.stop(self.started)
            for h in self.hosts:
                self.set(h, "cancelled", "run cancelled while starting containers")
            return False
        if not ok:
            self.group_reason = "container start failed on {}".format(self.first_failed)
            self.stop(self.started)
            for h in self.hosts:
                self.set(h, "stopped", "stopped: " + self.group_reason)
        return ok

    def stop(self, specs):
        groups = list(self.by_host(specs).values())
        pmap(lambda g: self.ops.stop(g[0].host, [s.name for s in g]), groups, self.ctx.parallel)

    def poll(self, specs):
        """{name: (status, rc)} for specs; hosts that cannot be reached are left out."""
        def one(item):
            host_name, group = item
            try:
                return host_name, self.ops.states(group[0].host, [s.name for s in group])
            except ConnectionError as e:
                return host_name, e
        out, lost = {}, {}
        for host_name, res in pmap(one, list(self.by_host(specs).items()), self.ctx.parallel):
            if isinstance(res, Exception):
                lost[host_name] = str(res)
            else:
                out.update(res)
        return out, lost

    def monitor(self):
        rt = self.rt
        deadline = time.time() + int(rt["run_timeout"])
        interval = float(rt["poll_interval"])
        running = list(self.started)
        launcher = None
        if self.job.mode == "mpi":
            launcher = self.start_launcher()
            if launcher is None:
                self.stop(running)
                return
        try:
            self._monitor_loop(running, launcher, deadline, interval)
        finally:
            if launcher is not None:
                if launcher.poll() is None:
                    launcher.kill()
                launcher.wait()
            log = getattr(self, "launcher_log", None)
            if log is not None and not log.closed:
                log.close()

    def _monitor_loop(self, running, launcher, deadline, interval):
        rt = self.rt
        misses = {}
        while True:
            if self.ctx.cancelled():
                self.end_all(running, "cancelled", "run cancelled", launcher)
                return
            if time.time() > deadline:
                self.group_reason = "run_timeout {}s reached".format(rt["run_timeout"])
                self.end_all(running, "timeout", self.group_reason, launcher)
                return
            states, lost = self.poll(running)
            for host_name, msg in lost.items():
                misses[host_name] = misses.get(host_name, 0) + 1
                if misses[host_name] >= LOST_AFTER:
                    self.set(host_name, "error", "lost connection: " + msg, fault=True)
                    self.first_failed = self.first_failed or host_name
            for host_name in set(self.hosts) - set(lost):
                misses.pop(host_name, None)
            still = []
            for spec in running:
                h = spec.host.name
                if h in self.result:
                    continue
                st, rc = states.get(spec.name, (None, None))
                if st in ("exited", "dead"):
                    if self.job.mode == "mpi":
                        self.set(h, "error", "sshd container exited early (exit {})".format(rc), rc, fault=True)
                        self.first_failed = self.first_failed or h
                    else:
                        self.set(h, status_of_rc(rc), "" if rc == 0 else "exit {}".format(rc), rc, fault=rc != 0)
                        if rc != 0:
                            self.first_failed = self.first_failed or h
                elif st == "missing":
                    self.set(h, "error", "container disappeared", fault=True)
                    self.first_failed = self.first_failed or h
                else:
                    still.append(spec)
            running = still
            if launcher is not None:
                rc = launcher.poll()
                if rc is not None:
                    self.finish_launcher(rc)
                    self.stop(running)
                    return
                if self.first_failed:
                    launcher.kill()
                    self.group_reason = "{} failed during the mpi run".format(self.first_failed)
                    self.end_all(running, "stopped", "stopped: " + self.group_reason, None)
                    return
            elif self.first_failed and running:
                self.group_reason = "{} failed ({})".format(self.first_failed,
                                                            self.result[self.first_failed]["reason"])
                self.end_all(running, "stopped", "stopped: " + self.group_reason, None)
                return
            if not running:
                return
            self.ctx.cancel.wait(interval)

    def end_all(self, running, status, reason, launcher):
        if launcher is not None and launcher.poll() is None:
            launcher.kill()
        self.stop(running)
        alone = len(self.hosts) == 1
        for spec in running:
            self.set(spec.host.name, status, reason, fault=status == "timeout" and alone)
        if status in ("cancelled", "timeout"):
            for h in self.hosts:
                self.set(h, status, reason)

    def start_launcher(self):
        master = self.job.containers[0]
        res = self.ops.exec_run(master.host, master.name,
                                peers_ready_script(self.nodes, self.rt["rendezvous_timeout"]),
                                timeout=int(self.rt["rendezvous_timeout"]) + 60)
        if not res.ok:
            self.group_reason = "mpi peers not ready: " + res.message()
            self.set(master.host.name, "error", self.group_reason, res.rc)
            self.first_failed = master.host.name
            return None
        path = os.path.join(self.ctx.local_dir(master.host.name, self.item.id), "launcher.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.launcher_log = open(path, "wb")
        try:
            return self.ops.exec_stream(master.host, master.name, self.job.launcher, self.job.launcher_env,
                                        self.launcher_log)
        except OSError as e:
            self.launcher_log.close()
            self.group_reason = "cannot start mpi launcher: {}".format(e)
            self.set(master.host.name, "error", self.group_reason)
            self.first_failed = master.host.name
            return None

    def finish_launcher(self, rc):
        master = self.job.containers[0].host.name
        if self.ctx.cancelled():
            for h in self.hosts:
                self.set(h, "cancelled", "run cancelled")
            return
        if rc in (0, 1):
            self.set(master, status_of_rc(rc), "" if rc == 0 else "launcher exit 1", rc)
        else:
            self.set(master, "error", "launcher exit {}".format(rc), rc)
        if rc != 0:
            self.first_failed = self.first_failed or master
            self.group_reason = "mpi launcher on {} exited {}".format(master, rc)

    def collect(self):
        def one(spec):
            host_name = spec.host.name
            remote = node_dir(self.ctx.tests, self.ctx.run_id, self.item.id, host_name)
            self.ops.save_logs(spec.host, spec.name, remote + "/container.log")
            res = self.ops.fetch(spec.host, remote, self.ctx.local_dir(host_name, self.item.id))
            return host_name, None if res.ok else res.message()
        for host_name, err in pmap(one, self.started, self.ctx.parallel):
            if err:
                self.copy_errors = getattr(self, "copy_errors", {})
                self.copy_errors[host_name] = err

    def cleanup(self):
        groups = list(self.by_host(getattr(self, "started", [])).values())
        pmap(lambda g: self.ops.remove(g[0].host, [s.name for s in g]), groups, self.ctx.parallel)

    # ---------- results ----------
    def finish(self, started, t0):
        finished = now_iso()
        copy_errors = getattr(self, "copy_errors", {})
        statuses = {}
        entries = {}
        container_names = {s.host.name: s.name for s in self.job.containers}
        for rank, n in enumerate(self.nodes):
            h = n.host.name
            res = self.result.get(h)
            own = h in container_names and (self.job.mode == "per-node" or rank == 0)
            if res is None:
                res = {"status": "pass", "reason": "", "rc": 0} if own else None
            path = os.path.join(self.ctx.local_dir(h, self.item.id), "summary.json")
            written = read_json(path) or {}
            if res is not None and res["status"] == "pass" and written.get("status") in ("fail", "error"):
                res = dict(res, status=written["status"], reason="summary.json reports " + written["status"])
                if own and self.job.mode == "per-node":
                    self.host_fault.add(h)
            if res is not None and h in copy_errors:
                res = dict(res, reason=(res["reason"] + "; " if res["reason"] else "") +
                           "results not copied: " + copy_errors[h])
                if res["status"] == "pass":
                    res["status"] = "error"
            statuses[h] = res
        group_status = worst([r["status"] for r in statuses.values() if r])
        for rank, n in enumerate(self.nodes):
            h = n.host.name
            res = statuses[h]
            group_result = h not in self.host_fault
            if res is None:
                res = {"status": group_status, "reason": "group result", "rc": None}
            summary = read_json(os.path.join(self.ctx.local_dir(h, self.item.id), "summary.json")) or {}
            summary.update({
                "item": self.item.id, "hostname": h, "run_id": self.ctx.run_id, "group": self.group.id,
                "rank": rank, "status": res["status"], "reason": res["reason"], "exit_code": res["rc"],
                "group_result": group_result, "container": container_names.get(h),
                "image": self.job.containers[0].image if self.job.containers else "",
                "image_digest": self.digests.get(h, ""), "started_at": started, "finished_at": finished,
            })
            summary.setdefault("metrics", {})
            summary.setdefault("checks", {})
            write_json(os.path.join(self.ctx.local_dir(h, self.item.id), "summary.json"), summary)
            entries[h] = {self.item.id: {"status": res["status"], "reason": res["reason"], "run_id": self.ctx.run_id,
                                         "group": self.group.id, "rank": rank, "group_result": group_result,
                                         "finished_at": finished}}
        reason = self.group_reason or ("" if group_status == "pass" else
                                       next((r["reason"] for r in statuses.values() if r and r["reason"]), ""))
        group = {"item": self.item.id, "group": self.group.id, "mode": self.job.mode, "status": group_status,
                 "reason": reason, "first_failed": self.first_failed, "hosts": [n.host.name for n in self.nodes],
                 "master_port": self.nodes[0].variables.get("master_port"), "started_at": started,
                 "finished_at": finished, "seconds": int(time.time() - t0), "run_id": self.ctx.run_id}
        write_json(os.path.join(self.ctx.run_dir, "_groups", self.item.id, self.group.id, "summary.json"), group)
        self.ctx.state.update(entries, (self.item.id, self.group.id, group))
        group["host_status"] = {h: (statuses[h] or {"status": group_status})["status"] for h in self.hosts}
        return group


def run_items(cluster, tests, selectors, only, exclude, ops, force=False, out=print, run_id=None):
    """Run enabled items in order. Returns (run_id, exit_code)."""
    from mtest.state import new_run_id
    run_id = run_id or new_run_id(tests.results_dir)
    ctx = RunContext(cluster, tests, run_id, ops, out)
    os.makedirs(ctx.run_dir, exist_ok=True)
    ctx.save_record()
    ctx.log("run {}  results: {}".format(run_id, ctx.run_dir))
    rt = tests.runtime
    overall = []
    try:
        for item, cfg in enabled_items(tests):
            if selectors and not any(matches(item.id, s) for s in selectors):
                continue
            if ctx.cancelled():
                break
            drop = {}
            if rt.get("stop_on_failure") and not force:
                bad = excluded_hosts(ctx.state.load(), item.id)
                for host, reason in sorted(bad.items()):
                    if host in cluster.by_name:
                        ctx.log("  {}: {} left out ({}); use --force to include".format(item.id, host, reason))
                drop = {item.id: set(bad)}
            plans = build_plan(cluster, tests, [item.id], only=only, exclude=exclude, run_id=run_id, drop=drop)
            plan = next((p for p in plans if p.item.id == item.id), None)
            if plan is None or not plan.groups:
                ctx.log("{}: no hosts to run".format(item.id))
                continue
            status = run_item(ctx, plan)
            if status:
                overall.append(status)
    except KeyboardInterrupt:
        ctx.cancel.set()
        ctx.log("interrupted; stopping containers ...")
        overall.append("cancelled")
    if ctx.cancelled():
        final = "cancelled"
    elif not overall:
        final = "empty"
    else:
        final = worst(overall)
    ctx.record.update({"status": final, "finished_at": now_iso()})
    ctx.save_record()
    ctx.log("run {} finished: {}".format(run_id, final if final != "empty" else "nothing was run"))
    if final in ("pass", "empty"):
        return run_id, 0
    return run_id, 130 if final == "cancelled" else 1


def run_item(ctx, plan):
    item = plan.item
    by_group = {}
    for n in plan.nodes:
        by_group.setdefault(n.group.id, []).append(n)
    jobs = []
    try:
        for g in plan.groups:
            jobs.append((g, by_group[g.id], build_job(ctx, item, plan.cfg, by_group[g.id])))
    except NotReady as e:
        ctx.log("{}: skipped, {}".format(item.id, e))
        ctx.record["items"][item.id] = {"status": "not_implemented", "reason": str(e)}
        ctx.save_record()
        return None
    scope = plan.cfg.get("scope", "node")
    workers = ctx.parallel if scope == "node" or ctx.tests.runtime["group_order"] == "parallel" else 1
    ctx.log("{}: {} host(s) in {} group(s), scope {}, mode {}".format(
        item.id, len(plan.nodes), len(plan.groups), scope, jobs[0][2].mode))
    entry = {"status": "running", "groups": {}, "hosts": [n.host.name for n in plan.nodes]}
    ctx.record["items"][item.id] = entry
    ctx.save_record()

    def one(job):
        g, nodes, j = job
        runner = GroupRunner(ctx, item, g, nodes, j)
        if ctx.cancelled():
            for n in nodes:
                runner.set(n.host.name, "cancelled", "run cancelled before start")
            result = runner.finish(now_iso(), time.time())
        else:
            result = runner.run()
        line = "  {} [{}] {} {}s".format(item.id, g.id, result["status"], result["seconds"])
        if result["status"] != "pass" and result["reason"]:
            line += "  " + result["reason"]
        ctx.log(line)
        with ctx._lock:
            entry["groups"][g.id] = {k: result[k] for k in ("status", "reason", "first_failed", "hosts",
                                                             "host_status", "seconds", "master_port")}
        ctx.save_record()
        return result["status"]

    try:
        run_cancellable(one, jobs, workers, ctx.cancel)
    finally:
        entry["status"] = worst([g["status"] for g in entry["groups"].values()]) if entry["groups"] else "cancelled"
        ctx.save_record()
    return entry["status"]
