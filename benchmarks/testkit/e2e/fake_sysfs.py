"""Build a fake /sys tree inside an e2e host so nic_discover sees BF3 and CX8 devices.

The real interfaces are docker veths; they are found by their IPv4 subnet:
  172.28.20.x -> BF3 port 0, 172.28.30.x -> BF3 port 1, 172.28.4K.x -> CX8 rail K (InfiniBand, ACTIVE, 800G).
Usage: python3 fake_sysfs.py /fake-sys
"""

import json
import os
import subprocess
import sys

root = sys.argv[1]
links = json.loads(subprocess.check_output(["ip", "-j", "addr"]).decode())
by_net = {}
for link in links:
    for a in link.get("addr_info", []):
        if a.get("family") == "inet":
            by_net[".".join(a["local"].split(".")[:3])] = link["ifname"]


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def device(addr, parent, dev_id, numa, netdev, ibdev, link_layer):
    path = os.path.join(root, "devices", parent, addr)
    for name, value in (("vendor", "0x15b3"), ("device", "0x" + dev_id), ("numa_node", str(numa))):
        w(os.path.join(path, name), value)
    os.makedirs(os.path.join(path, "net", netdev), exist_ok=True)
    os.makedirs(os.path.join(path, "infiniband", ibdev), exist_ok=True)
    bus = os.path.join(root, "bus/pci/devices")
    os.makedirs(bus, exist_ok=True)
    if not os.path.lexists(os.path.join(bus, addr)):
        os.symlink(path, os.path.join(bus, addr))
    with open("/sys/class/net/{}/address".format(netdev)) as f:
        w(os.path.join(root, "class/net", netdev, "address"), f.read().strip())
    port = os.path.join(root, "class/infiniband", ibdev, "ports/1")
    w(os.path.join(port, "state"), "4: ACTIVE")
    w(os.path.join(port, "rate"), "800 Gb/sec (4X XDR)" if link_layer == "InfiniBand" else "400 Gb/sec")
    w(os.path.join(port, "link_layer"), link_layer)


device("0009:01:00.0", "pci0009:00", "a2dc", 0, by_net["172.28.20"], "mlx5_4", "Ethernet")
device("0009:01:00.1", "pci0009:00", "a2dc", 0, by_net["172.28.30"], "mlx5_5", "Ethernet")
for k in range(4):
    device("000{}:03:00.0".format(k), "pci000{}:00/000{}:00:00.0".format(k, k), "1023", k // 2,
           by_net["172.28.4{}".format(k)], "mlx5_{}".format(k), "InfiniBand")
print("fake sysfs ready:", json.dumps(by_net))
