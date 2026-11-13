"""Run on a host (python3, stdlib only): report GPUs, Mellanox NICs, the LOM port and the serial as JSON.

testkit sends this file over ssh stdin. Arguments come in TESTKIT_ARGS (base64 JSON):
  cx8_ids, bf3_ids   PCI device IDs (hex, without vendor) of compute NICs and BlueField-3
  nic_pcis           PCI addresses of other NICs to report (mgmt / storage on a plain NIC)
  mlxconfig          query firmware settings with mlxconfig when it is installed
  sysfs, fake_gpus   test hooks: another /sys root, and GPU PCI addresses instead of nvidia-smi
"""

import base64
import json
import os
import re
import shutil
import subprocess

ROOT = "/sys"
PCI = "/sys/bus/pci/devices"
FAKE_GPUS = None


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def norm_pci(addr):
    """00000008:01:00.0 -> 0008:01:00.0"""
    dom, _, rest = addr.strip().lower().partition(":")
    return "{:04x}:{}".format(int(dom, 16), rest) if rest else addr.lower()


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def gpus():
    if FAKE_GPUS is not None:
        return [{"index": i, "pci": norm_pci(p)} for i, p in enumerate(FAKE_GPUS)]
    if not shutil.which("nvidia-smi"):
        return []
    rc, out = run(["nvidia-smi", "--query-gpu=index,pci.bus_id,serial", "--format=csv,noheader"])
    if rc != 0:
        return []
    out_list = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            out_list.append({"index": int(parts[0]), "pci": norm_pci(parts[1]),
                             "serial": parts[2] if len(parts) > 2 else ""})
    return out_list


def pci_path(addr):
    """Path of the device in the PCI tree, used to find the NIC closest to each GPU."""
    return os.path.realpath(os.path.join(PCI, addr))


def common_depth(a, b):
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n


def port(ibdev):
    base = os.path.join(ROOT, "class/infiniband", ibdev, "ports/1")
    state = read(os.path.join(base, "state"))          # "4: ACTIVE"
    rate = read(os.path.join(base, "rate"))            # "800 Gb/sec (4X XDR)"
    m = re.match(r"\s*([\d.]+)", rate)
    return {"link_layer": read(os.path.join(base, "link_layer")),
            "state": state.split(":")[-1].strip() if state else "",
            "rate_gbps": float(m.group(1)) if m else None, "rate": rate,
            "guid": read(os.path.join(ROOT, "class/infiniband", ibdev, "node_guid"))}


def mlx_query(pci, param):
    """(current, next_boot) of a firmware parameter, or (None, None)."""
    rc, out = run(["mlxconfig", "-d", pci, "-e", "q", param], timeout=120)
    if rc != 0:
        return None, None
    for line in out.splitlines():
        tokens = line.replace("*", " ").split()
        if tokens and tokens[0] == param:
            if len(tokens) >= 4:
                return tokens[2], tokens[3]
            return tokens[-1], tokens[-1]
    return None, None


def devices(ids):
    out = []
    for addr in listdir(PCI):
        base = os.path.join(PCI, addr)
        if read(os.path.join(base, "vendor")).lower() != "0x15b3":
            continue
        dev = read(os.path.join(base, "device")).lower().replace("0x", "")
        if dev not in ids:
            continue
        netdevs = listdir(os.path.join(base, "net"))
        ibdevs = listdir(os.path.join(base, "infiniband"))
        numa = read(os.path.join(base, "numa_node"), "-1")
        item = {"pci": addr, "device": dev, "numa": int(numa) if numa.lstrip("-").isdigit() else -1,
                "netdev": netdevs[0] if netdevs else "", "ibdev": ibdevs[0] if ibdevs else "",
                "mac": read(os.path.join(ROOT, "class/net", netdevs[0], "address")) if netdevs else ""}
        if item["ibdev"]:
            item.update(port(item["ibdev"]))
        out.append(item)
    return out


def nics(addrs):
    """Network devices at the given PCI addresses, any vendor."""
    out = []
    for addr in addrs:
        base = os.path.join(PCI, addr)
        if not os.path.isdir(base):
            continue
        netdevs = listdir(os.path.join(base, "net"))
        out.append({"pci": addr, "vendor": read(os.path.join(base, "vendor")).lower().replace("0x", ""),
                    "device": read(os.path.join(base, "device")).lower().replace("0x", ""),
                    "netdev": netdevs[0] if netdevs else "",
                    "mac": read(os.path.join(ROOT, "class/net", netdevs[0], "address")) if netdevs else ""})
    return out


def lom():
    """The interface that carries this ssh session, and its gateway."""
    conn = os.environ.get("SSH_CONNECTION", "").split()
    client, server = (conn[0], conn[2]) if len(conn) >= 4 else ("", "")
    info = {"control_ip": client, "ip": server, "if": "", "mac": "", "gateway": ""}
    rc, out = run(["ip", "-j", "addr"])
    if rc == 0 and server:
        for link in json.loads(out or "[]"):
            for a in link.get("addr_info", []):
                if a.get("local") == server:
                    info["if"] = link["ifname"]
                    info["mac"] = link.get("address", "")
                    info["prefix"] = "{}/{}".format(server, a.get("prefixlen"))
    # The default route via the LOM, or (when the LOM default route is in a policy table) any LOM route with a gateway.
    for cmd in (["ip", "-j", "route", "show", "default"],
                ["ip", "-j", "route", "show", "table", "all", "dev", info["if"] or "lo"]):
        rc, out = run(cmd)
        if rc != 0:
            continue
        for r in json.loads(out or "[]"):
            if r.get("gateway") and r.get("dev", info["if"]) == info["if"]:
                info["gateway"] = r["gateway"]
                return info
    return info


def serial():
    """System serial number from DMI (sysfs, else dmidecode); empty when unknown."""
    value = read(os.path.join(ROOT, "class/dmi/id/product_serial"))
    if not value and ROOT == "/sys" and shutil.which("dmidecode"):
        rc, out = run(["dmidecode", "-s", "system-serial-number"])
        value = out.strip() if rc == 0 else ""
    return "" if value.lower() in ("", "not specified", "to be filled by o.e.m.", "default string") else value


def configure(args):
    global ROOT, PCI, FAKE_GPUS
    ROOT = args.get("sysfs") or "/sys"
    PCI = os.path.join(ROOT, "bus/pci/devices")
    FAKE_GPUS = args.get("fake_gpus")


def discover(args):
    configure(args)
    cx8_ids = {i.lower() for i in args.get("cx8_ids", ["1023"])}
    bf3_ids = {i.lower() for i in args.get("bf3_ids", ["a2dc"])}
    use_mlx = args.get("mlxconfig", True) and shutil.which("mlxconfig") is not None
    gpu_list = gpus()
    cx8 = devices(cx8_ids)
    bf3 = sorted(devices(bf3_ids), key=lambda d: d["pci"])

    # Rail N = the compute NIC closest to GPU N in the PCI tree; without GPUs, PCI order.
    rails, free = [], list(cx8)
    for g in sorted(gpu_list, key=lambda g: g["index"]):
        if not free:
            break
        gp = pci_path(g["pci"])
        best = max(free, key=lambda d: (common_depth(gp, pci_path(d["pci"])), -cx8.index(d)))
        free.remove(best)
        rails.append(dict(best, gpu=g["index"], gpu_pci=g["pci"]))
    for d in sorted(free, key=lambda d: d["pci"]):
        rails.append(dict(d, gpu=None, gpu_pci=""))
    for i, r in enumerate(rails):
        r["rail"] = i
        if use_mlx:
            r["link_type"], r["link_type_next"] = mlx_query(r["pci"], "LINK_TYPE_P1")

    bf3_info = {"pfs": bf3}
    if bf3 and use_mlx:
        bf3_info["mode"], bf3_info["mode_next"] = mlx_query(bf3[0]["pci"], "INTERNAL_CPU_OFFLOAD_ENGINE")

    return {"gpus": gpu_list, "rails": rails, "bf3": bf3_info, "nics": nics(args.get("nic_pcis") or []),
            "lom": lom(), "mlxconfig": use_mlx, "hostname": os.uname()[1], "serial": serial()}


if __name__ == "__main__":
    print(json.dumps(discover(json.loads(base64.b64decode(os.environ.get("TESTKIT_ARGS", "e30=")).decode()))))
