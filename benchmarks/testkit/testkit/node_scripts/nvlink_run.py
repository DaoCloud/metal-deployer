#!/usr/bin/env python3
"""S7b NVLink steps on the rank 0 host: dcgmi mndiag (mnnvbandwidth / nvloom), one step after another.

Started by testkit with systemd-run so it survives the ssh session; testkit polls <out>/done.json.
Usage: python3 nvlink_run.py <args.json>
args.json: {"out": result dir, "log_dir": DCGM_HOME_DIR, "stop_on_fail": bool,
            "steps": [{"name", "kind": "pairs" | "mndiag", "argv": [...], "timeout": seconds}]}
Writes <out>/raw/<name>.txt (stdout), <name>.err, <name>-logs/ (workload logs written during the step)
and <out>/done.json. A step that does not pass stops the later steps when stop_on_fail is set.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

MAX_COPY = 200 * 1024 * 1024


def pairs_verdict(text):
    m = re.search(r"Pairs Failed\D*(\d+)", text)
    planned = re.search(r"Pairs Planned\D*(\d+)", text)
    if not m or not planned:
        return "error", "no pair counts in the output"
    if int(m.group(1)) or int(planned.group(1)) == 0:
        return "fail", "{} of {} pairs failed".format(m.group(1), planned.group(1))
    return "pass", "{} pairs passed".format(planned.group(1))


def json_verdict(text):
    start = text.find("{")
    try:
        doc = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return "error", "no JSON output"
    if doc.get("runtime_error"):
        return "error", str(doc["runtime_error"])[:300]
    tests = [t for c in doc.get("test_categories") or [] for t in c.get("tests") or []]
    if not tests:
        return "error", "no test result"
    status = [str(t.get("status") or (t.get("test_summary") or {}).get("status") or "").lower() for t in tests]
    if all(s == "pass" for s in status):
        return "pass", "Pass"
    if all(s == "skip" for s in status):
        return "skip", "Skip"
    return "fail", ", ".join(status)


def copy_logs(log_dir, since, dest, text):
    """Copy files written under log_dir during the step, and absolute paths named in the output."""
    paths = set()
    for root, _, files in os.walk(log_dir or "/nonexistent"):
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getmtime(p) >= since - 1 and os.path.getsize(p) <= MAX_COPY:
                    paths.add(p)
            except OSError:
                pass
    for p in re.findall(r"(/[\w./-]+)", text):
        if os.path.isfile(p) and not p.startswith(("/proc", "/sys", "/dev")):
            try:
                if os.path.getsize(p) <= MAX_COPY:
                    paths.add(p)
            except OSError:
                pass
    for p in sorted(paths):
        target = os.path.join(dest, p.lstrip("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            shutil.copyfile(p, target)
        except OSError:
            pass


def main():
    with open(sys.argv[1]) as f:
        a = json.load(f)
    raw = os.path.join(a["out"], "raw")
    os.makedirs(raw, exist_ok=True)
    results = []
    stop = False
    for step in a["steps"]:
        name = step["name"]
        if stop:
            results.append({"name": name, "verdict": "skip", "reason": "an earlier step did not pass"})
            continue
        t0 = time.time()
        with open(os.path.join(raw, name + ".cmd"), "w") as f:
            f.write(" ".join(step["argv"]) + "\n")
        try:
            p = subprocess.run(step["argv"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=int(step.get("timeout") or 7200))
            rc, out, err = p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
        except subprocess.TimeoutExpired as e:
            rc, out, err = 124, (e.stdout or b"").decode(errors="replace"), "timeout after {}s".format(step["timeout"])
        except OSError as e:
            rc, out, err = 127, "", str(e)
        with open(os.path.join(raw, name + ".txt"), "w") as f:
            f.write(out)
        with open(os.path.join(raw, name + ".err"), "w") as f:
            f.write(err)
        copy_logs(a.get("log_dir"), t0, os.path.join(raw, name + "-logs"), out)
        if rc != 0:
            verdict, reason = "error", "dcgmi exit {}: {}".format(rc, (err or out).strip()[-300:])
        elif step.get("kind") == "pairs":
            verdict, reason = pairs_verdict(out)
        else:
            verdict, reason = json_verdict(out)
        results.append({"name": name, "rc": rc, "verdict": verdict, "reason": reason,
                        "seconds": int(time.time() - t0)})
        if verdict not in ("pass", "skip") and a.get("stop_on_fail", True):
            stop = True
    tmp = os.path.join(a["out"], "done.json.tmp")
    with open(tmp, "w") as f:
        json.dump({"steps": results}, f)
    os.replace(tmp, os.path.join(a["out"], "done.json"))


if __name__ == "__main__":
    main()
