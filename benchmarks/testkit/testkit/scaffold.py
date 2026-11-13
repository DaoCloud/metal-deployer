"""Packaged templates for "testkit init" and the key schema used by "testkit validate"."""

import os

import yaml

from testkit import platforms

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _read(name, platform=None):
    parts = [TEMPLATE_DIR] + ([platform] if platform else []) + [name]
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def host_entry(rack_index, rack, tray, platform=platforms.DEFAULT):
    """network.yaml text for one host; the addresses are only placeholders to edit."""
    r, t = rack_index, tray
    example = platforms.PLATFORMS[platform]["example"]
    lines = [
        "  {}-{}-t{:02d}:".format(platform, rack.lower(), t),
        "    rack: {}".format(rack),
        "    tray: {}".format(t),
        "    lom: {{ip: 10.10.{}.{}/16, gateway: 10.10.0.1, policy: {{table: 100, priority: 100}}}}".format(r, 10 + t),
        '    mgmt: {{pci: "{}", ip: 10.20.{}.{}/16, gateway: 10.20.0.1, default_route: true, dns: [10.20.0.53]}}'
        .format(example["mgmt_pci"], r, 10 + t),
        "    storage:",
        '      pci: "{}"'.format(example["storage_pci"]),
        "      ip: 10.30.{}.{}/16".format(r, 10 + t),
        "      mtu: 9000",
        "      routes: [{to: 10.31.0.0/16, via: 10.30.0.1}]",
        "    compute:",
        "      link_layer: infiniband",
        "      mtu: 4092",
        '      pkey: "0x7fff"',
        "      devices:",
    ]
    for k, pci in enumerate(example["ib_pci"]):
        lines.append('        - {{pci: "{}", ip: 10.10{}.{}.{}/16}}'.format(pci, k, r, 10 + t))
    return "\n".join(lines)


def network_text(racks=1, trays=2, platform=platforms.DEFAULT):
    entries = [host_entry(ri, "R{}".format(ri + 1), t, platform=platform)
               for ri in range(racks) for t in range(1, trays + 1)]
    return _read("network.yaml", platform).replace("__HOSTS__", "\n".join(entries))


def tests_text(platform=platforms.DEFAULT):
    return _read("tests.yaml", platform)



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
