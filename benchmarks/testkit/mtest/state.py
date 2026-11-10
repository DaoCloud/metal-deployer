"""Local state under the results directory.

<results_dir>/
  state.json                  latest result of every (host, item) and every group
  locks/<host>.lock           which run currently uses a host
  nic/<host>.json             S3 NIC discovery result
  <run_id>/run.json           one "mtest run" invocation: items, groups, ranks, status
  <run_id>/<host>/<item>/     files copied back from the host, summary.json
  <run_id>/_groups/<item>/<group>/summary.json
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager


def nic_path(results_dir, hostname):
    return os.path.join(results_dir, "nic", hostname + ".json")


def load_nic(results_dir, hostname):
    """NIC discovery result of one host (written by "mtest nic discover"), or None."""
    path = nic_path(results_dir, hostname)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    """Write atomically so readers never see half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "{}.{}.tmp".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path, default=None):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError:
            return default


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id(results_dir):
    base = time.strftime("%Y%m%d-%H%M%S")
    run_id, n = base, 1
    while os.path.exists(os.path.join(results_dir, run_id)):
        n += 1
        run_id = "{}-{}".format(base, n)
    return run_id


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


@contextmanager
def file_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class StateStore:
    """state.json: {"hosts": {host: {item: entry}}, "groups": {item: {group: entry}}}.

    Several mtest processes may update it (different host sets), so every update is a
    locked read-modify-write.
    """

    def __init__(self, results_dir):
        self.path = os.path.join(results_dir, "state.json")
        self.lock_path = os.path.join(results_dir, "state.lock")

    def load(self):
        data = read_json(self.path, {}) or {}
        data.setdefault("hosts", {})
        data.setdefault("groups", {})
        return data

    def update(self, host_entries=None, group_entry=None):
        """host_entries: {host: {item: entry}}; group_entry: (item, group, entry)."""
        with file_lock(self.lock_path):
            data = self.load()
            for host, items in (host_entries or {}).items():
                data["hosts"].setdefault(host, {}).update(items)
            if group_entry:
                item, group, entry = group_entry
                data["groups"].setdefault(item, {})[group] = entry
            write_json(self.path, data)


class HostLocks:
    """One lock file per host, so two runs never use the same host at the same time.

    Every change happens while holding locks/.guard, so taking over a stale lock (its
    process is gone) cannot race with another mtest process.
    """

    def __init__(self, results_dir):
        self.dir = os.path.join(results_dir, "locks")
        self.guard = os.path.join(self.dir, ".guard")

    def path(self, name):
        return os.path.join(self.dir, name + ".lock")

    def holder(self, name):
        """Lock info if a live process holds the host, else None."""
        info = read_json(self.path(name))
        if info and pid_alive(info.get("pid")):
            return info
        return None

    def acquire(self, names, run_id, item):
        """Lock all names or none. Returns {name: holder_info} of conflicts (empty on success)."""
        with file_lock(self.guard):
            conflicts = {}
            for name in names:
                info = self.holder(name)
                if info is not None:
                    conflicts[name] = info
            if conflicts:
                return conflicts
            mine = {"run_id": run_id, "item": item, "pid": os.getpid(), "since": now_iso()}
            for name in names:
                write_json(self.path(name), mine)
            return {}

    def release(self, names, run_id=None):
        """Remove locks of names held by this process (or by run_id when given)."""
        with file_lock(self.guard):
            for name in names:
                info = read_json(self.path(name)) or {}
                own = info.get("run_id") == run_id if run_id else info.get("pid") == os.getpid()
                if own:
                    try:
                        os.unlink(self.path(name))
                    except FileNotFoundError:
                        pass
