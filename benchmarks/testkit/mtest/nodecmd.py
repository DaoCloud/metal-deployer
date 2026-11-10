"""Run the bundled node scripts (mtest/node_scripts/*.py) on hosts over ssh."""

import base64
import json
import os

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_scripts")

# Test hooks: let the end-to-end test point node scripts at a fake sysfs and fake GPUs.
TEST_ARGS = {"sysfs": os.environ.get("MTEST_NODE_SYSFS"), "fake_gpus": os.environ.get("MTEST_NODE_FAKE_GPUS")}


class NodeError(Exception):
    pass


def script(name):
    with open(os.path.join(SCRIPT_DIR, name), "rb") as f:
        return f.read()


def run_script(remote, host, name, args, timeout=300):
    """Run node_scripts/<name> with args on host; returns its JSON output or raises NodeError."""
    args = dict(args)
    for key, value in TEST_ARGS.items():
        if value and key not in args:
            args[key] = json.loads(value) if key == "fake_gpus" else value
    payload = base64.b64encode(json.dumps(args).encode()).decode()
    res = remote.run(host, "MTEST_ARGS={} python3 -".format(payload), timeout=timeout, input=script(name))
    if not res.ok:
        raise NodeError("{} failed (exit {}): {}".format(name, res.rc, res.message()))
    lines = [l for l in res.out.strip().splitlines() if l.startswith("{")]
    if not lines:
        raise NodeError("{}: no JSON output: {}".format(name, res.out[-300:]))
    try:
        return json.loads(lines[-1])
    except ValueError as e:
        raise NodeError("{}: bad JSON output: {}".format(name, e))
