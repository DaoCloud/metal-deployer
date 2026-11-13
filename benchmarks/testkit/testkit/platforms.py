"""Hardware platforms. cluster.yaml and tests.yaml name one with the top-level "platform" key.

A platform decides the templates "testkit init" writes (templates/<platform>/) and the defaults of
the network section: network.expect and the NIC that carries each network role. Keys written under
network in cluster.yaml win over the platform defaults.
  gb300   BlueField-3 port 0 = mgmt, port 1 = storage; 4 ConnectX-8 rails on InfiniBand; 4 GPUs
"""

from testkit.util import dig, deep_merge

DEFAULT = "gb300"

PLATFORMS = {
    "gb300": {
        "network": {
            "expect": {"gpus": 4, "cx8": {"count": 4, "pci_id": "15b3:1023", "link_layer": "InfiniBand",
                                          "rate_gbps": 800},
                       "bf3": {"pf_count": 2, "pci_id": "15b3:a2dc", "mode": "nic"}, "compare_golden": True},
            "mgmt": {"device": "bf3", "port": 0},
            "storage": {"device": "bf3", "port": 1},
        },
        # PCI addresses of the example hosts written by "testkit init".
        "example": {"mgmt_pci": "0009:01:00.0", "storage_pci": "0009:01:00.1",
                    "ib_pci": ["0000:03:00.0", "0001:03:00.0", "0002:03:00.0", "0003:03:00.0"]},
    },
}

# bf3: a BlueField-3 PF (port 0/1); nic: any network device, found by its PCI address in the IP plan;
# lom: the mgmt address is the LOM address, S4 leaves mgmt alone.
DEVICES = {"mgmt": ("bf3", "nic", "lom"), "storage": ("bf3", "nic")}


def names():
    return ", ".join(sorted(PLATFORMS))


def check(name, where, problems):
    """Report a missing or unknown platform; returns the name when it is supported, else None."""
    if name in (None, ""):
        problems.error("{}: platform is required (one of: {}), e.g. 'platform: {}'".format(where, names(), DEFAULT))
        return None
    if name not in PLATFORMS:
        problems.error("{}: platform '{}' is not supported (one of: {})".format(where, name, names()))
        return None
    return name


def resolve(platform, network):
    """network section with the platform defaults filled in; an unknown platform adds nothing."""
    network = network or {}
    base = (PLATFORMS.get(platform) or {}).get("network")
    return deep_merge(base, network) if base else dict(network)


def rails(network):
    """Number of compute rails (IB ports) of the resolved network section."""
    return dig(network, "expect.cx8.count", 4)


def device(network, role):
    """NIC kind of a role (mgmt or storage)."""
    return ((network or {}).get(role) or {}).get("device") or "bf3"
