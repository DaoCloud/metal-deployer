"""Hardware profiles: defaults for network.expect and the NIC that carries each network role.

cluster.yaml selects one with network.profile; keys written under network win over the profile.
  gb300   BlueField-3 port 0 = mgmt, port 1 = storage; 4 ConnectX-8 rails on InfiniBand
  custom  nothing is assumed: mgmt / storage are plain NICs found by mgmt_pci / storage_pci
          (or mgmt stays on the LOM), and expect describes the rails
"""

from testkit.util import deep_merge

DEFAULT = "gb300"

PROFILES = {
    "gb300": {
        "expect": {"gpus": 4, "cx8": {"count": 4, "pci_id": "15b3:1023", "link_layer": "InfiniBand", "rate_gbps": 800},
                   "bf3": {"pf_count": 2, "pci_id": "15b3:a2dc", "mode": "nic"}, "compare_golden": True},
        "mgmt": {"device": "bf3", "port": 0},
        "storage": {"device": "bf3", "port": 1},
    },
    "custom": {
        "mgmt": {"device": "nic"},
        "storage": {"device": "nic"},
    },
}

# bf3: a BlueField-3 PF (port 0/1); nic: any network device, found by its PCI address in the IP plan;
# lom: the mgmt address is the LOM address, S4 leaves mgmt alone.
DEVICES = {"mgmt": ("bf3", "nic", "lom"), "storage": ("bf3", "nic")}


def resolve(network):
    """network section with the profile defaults filled in; unknown profiles resolve to the raw section."""
    network = network or {}
    base = PROFILES.get(network.get("profile") or DEFAULT)
    return deep_merge(base, network) if base is not None else dict(network)


def device(network, role):
    """NIC kind of a role (mgmt or storage)."""
    return ((network or {}).get(role) or {}).get("device") or "bf3"
