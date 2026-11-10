"""Packaged templates for "mtest init" and the key schema used by "mtest validate"."""

import os

import yaml

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _read(name):
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


def host_entry(rack_index, rack, tray, golden=False, examples=False):
    """YAML text for one host; the address plan is only a placeholder to edit."""
    r, t = rack_index, tray
    lines = [
        "  - name: gb300-{}-t{:02d}".format(rack.lower(), t),
        "    rack: {}".format(rack),
        "    tray: {}".format(t),
        '    serial: ""',
        "    ssh: 10.10.{}.{}".format(r, 10 + t),
        "    mgmt: 10.20.{}.{}/16".format(r, 10 + t),
        "    storage: 10.30.{}.{}/16".format(r, 10 + t),
        "    ib: [{}]".format(", ".join("10.10{}.{}.{}/16".format(k, r, 10 + t) for k in range(4))),
    ]
    if golden:
        lines.append("    tags: [golden]")
    if examples:
        lines.append("    # env: {NCCL_DEBUG: INFO}")
        lines.append("    # skip: [moe]")
    return "\n".join(lines)


def cluster_text(racks=1, trays=2):
    entries = []
    for ri in range(racks):
        rack = "R{}".format(ri + 1)
        for t in range(1, trays + 1):
            first = ri == 0 and t == 1
            second = ri == 0 and t == 2
            entries.append(host_entry(ri, rack, t, golden=first, examples=second))
    return _read("cluster.yaml").replace("__HOSTS__", "\n".join(entries))


def tests_text():
    return _read("tests.yaml")


def cluster_schema():
    data = yaml.safe_load(cluster_text(1, 1))
    host = dict(data["hosts"][0])
    host.update({"tags": [], "env": {}, "skip": [], "gpus": 4})
    data["hosts"] = host
    return data


def tests_schema():
    data = yaml.safe_load(tests_text())
    # Keys every item section may carry even if the template does not show them.
    extra = {"enabled": True, "scope": "", "hosts": "", "groups": [], "env": {}, "args": [],
             "image": "", "cmd": [], "per_rank": {}, "benchmarks": {}, "config": ""}
    for section in ("superbench", "sharp", "moe"):
        for sub in data[section].values():
            for key, value in extra.items():
                sub.setdefault(key, value)
    for section in ("dcgm", "imex"):
        for key, value in extra.items():
            data[section].setdefault(key, value)
    return data
