"""Packaged templates for "testkit init" and the key schema used by "testkit validate"."""

import os

import yaml

from testkit import platforms

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _read(name, platform=None):
    parts = [TEMPLATE_DIR] + ([platform] if platform else []) + [name]
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def host_entry(rack_index, rack, tray, golden=False, examples=False, platform=platforms.DEFAULT):
    """YAML text for one host; the address plan is only a placeholder to edit."""
    r, t = rack_index, tray
    example = platforms.PLATFORMS[platform]["example"]
    rails = len(example["ib_pci"])
    lines = [
        "  - name: {}-{}-t{:02d}".format(platform, rack.lower(), t),
        "    rack: {}".format(rack),
        "    tray: {}".format(t),
        '    serial: ""',
        "    ssh: 10.10.{}.{}".format(r, 10 + t),
        "    mgmt: 10.20.{}.{}/16".format(r, 10 + t),
        "    storage: 10.30.{}.{}/16".format(r, 10 + t),
        "    ib: [{}]".format(", ".join("10.10{}.{}.{}/16".format(k, r, 10 + t) for k in range(rails))),
        '    mgmt_pci: "{}"'.format(example["mgmt_pci"]),
        '    storage_pci: "{}"'.format(example["storage_pci"]),
        "    ib_pci: [{}]".format(", ".join('"{}"'.format(p) for p in example["ib_pci"])),
    ]
    if golden:
        lines.append("    tags: [golden]")
    if examples:
        lines.append("    # env: {NCCL_DEBUG: INFO}")
        lines.append("    # skip: [moe]")
    return "\n".join(lines)


def cluster_text(racks=1, trays=2, platform=platforms.DEFAULT):
    entries = []
    for ri in range(racks):
        rack = "R{}".format(ri + 1)
        for t in range(1, trays + 1):
            first = ri == 0 and t == 1
            second = ri == 0 and t == 2
            entries.append(host_entry(ri, rack, t, golden=first, examples=second, platform=platform))
    return _read("cluster.yaml", platform).replace("__HOSTS__", "\n".join(entries))


def tests_text(platform=platforms.DEFAULT):
    return _read("tests.yaml", platform)


def cluster_schema(platform=platforms.DEFAULT):
    data = yaml.safe_load(cluster_text(1, 1, platform))
    host = dict(data["hosts"][0])
    from testkit.config import PLAN_FIELDS
    host.update({"tags": [], "env": {}, "skip": [], "gpus": 4})
    host.update({k: None for k in PLAN_FIELDS})
    data["hosts"] = host
    return data


def tests_schema(platform=platforms.DEFAULT):
    data = yaml.safe_load(tests_text(platform))
    # Keys every item section may carry even if the template does not show them.
    extra = {"enabled": True, "scope": "", "hosts": "", "groups": [], "env": {}, "args": [],
             "image": "", "cmd": [], "per_rank": {}, "benchmarks": {"include": [], "exclude": []}, "config": "", "timeout": 0,
             "gate": True, "full_rack": True, "baseline": {}, "outlier_pct": 0, "outlier_action": "",
             "outlier": {"pct": 0, "action": "", "metrics": [], "rdma_tests": []},
             "ib_pairs": "", "cross_rack_rounds": "", "ib_benchmarks": [], "pairs_dir": "",
             "hca_check": "", "small_message_bytes": []}
    for section in ("superbench", "sharp", "moe", "nvlink", "nvshmem", "nixl"):
        for sub in data[section].values():
            for key, value in extra.items():
                sub.setdefault(key, value)
    for section in ("smoke", "dcgm", "imex", "links"):
        for key, value in extra.items():
            data[section].setdefault(key, value)
    return data
