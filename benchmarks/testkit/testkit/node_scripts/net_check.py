"""Run on a host (python3, stdlib only): read-only check of the network against network.yaml.

testkit net-check sends this file over ssh stdin; TESTKIT_ARGS (base64 JSON) holds the expected
state of this host (built by testkit/netcheck.py). Nothing is changed on the host.
Prints one JSON object: {"ok": bool, "checks": [{"name", "result": pass|fail|warn|skip, "detail"}]}.
"""

import base64
import json
import os
import shutil
import socket
import subprocess

ROOT = "/sys"


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def ip_json(*args):
    rc, out = run(["ip", "-j"] + list(args))
    if rc != 0:
        return []
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


class Checks:

    def __init__(self):
        self.items = []

    def add(self, name, result, detail=""):
        self.items.append({"name": name, "result": result, "detail": detail})

    def judge(self, name, problems, ok_detail):
        self.add(name, "fail" if problems else "pass", "; ".join(problems) if problems else ok_detail)


def links():
    """ifname -> ip -j addr entry."""
    return {l["ifname"]: l for l in ip_json("addr", "show")}


def addrs_of(link):
    return ["{}/{}".format(a["local"], a["prefixlen"]) for a in link.get("addr_info", [])
            if a.get("local") and a.get("family") in ("inet", "inet6")]


def holder(all_links, cidr):
    """Interface that carries this address (CIDR), or None."""
    for name, link in all_links.items():
        if cidr in addrs_of(link):
            return name
    return None


def link_layer(netdev, ibdev, link):
    if ibdev:
        value = read(os.path.join(ROOT, "class/infiniband", ibdev, "ports/1/link_layer"))
        if value:
            return value.lower()
    return "infiniband" if (link or {}).get("link_type") == "infiniband" else "ethernet"


def check_port(c, port, all_links):
    """PCI -> netdev, link layer, link state, address, MTU, pkey. Returns the netdev or None."""
    name, pci = port["label"], port["pci"]
    base = os.path.join(ROOT, "bus/pci/devices", pci)
    if not os.path.isdir(base):
        c.add(name, "fail", "PCI {} not found on this host".format(pci))
        return None
    netdevs = listdir(os.path.join(base, "net"))
    ibdevs = listdir(os.path.join(base, "infiniband"))
    if not netdevs:
        c.add(name, "fail", "PCI {} has no network interface (driver not loaded?)".format(pci))
        return None
    dev, ibdev = netdevs[0], ibdevs[0] if ibdevs else ""
    link = all_links.get(dev)
    if link is None:
        c.add(name, "fail", "PCI {}: interface {} not shown by ip".format(pci, dev))
        return None
    problems = []
    ll = link_layer(dev, ibdev, link)
    if ll != port["link_layer"]:
        problems.append("link layer {} (expected {})".format(ll, port["link_layer"]))
    if link.get("operstate") == "DOWN" or "UP" not in (link.get("flags") or []):
        problems.append("link {}".format(link.get("operstate", "down")))
    if ibdev and ll == "infiniband":
        state = read(os.path.join(ROOT, "class/infiniband", ibdev, "ports/1/state")).split(":")[-1].strip()
        if state and state != "ACTIVE":
            problems.append("IB port {} {}".format(ibdev, state))
    have = addrs_of(link)
    if port["ip"] not in have:
        elsewhere = holder(all_links, port["ip"])
        problems.append("address {} missing (has {}){}".format(
            port["ip"], ",".join(have) or "none", "; it is on {}".format(elsewhere) if elsewhere else ""))
    if port.get("mtu") and link.get("mtu") != port["mtu"]:
        problems.append("mtu {} (expected {})".format(link.get("mtu"), port["mtu"]))
    if port.get("pkey"):
        value = read(os.path.join(ROOT, "class/net", dev, "pkey"))
        if not value:
            problems.append("pkey not readable (expected {})".format(port["pkey"]))
        elif int(value, 16) | 0x8000 != int(port["pkey"], 16) | 0x8000:   # 0x8000 = full membership bit
            problems.append("pkey {} (expected {})".format(value, port["pkey"]))
    c.judge(name, problems, "{} {}{} {} mtu {}".format(
        pci, dev, "/" + ibdev if ibdev else "", port["ip"], link.get("mtu")))
    return dev


def check_lom(c, lom, all_links):
    dev = holder(all_links, lom["ip"])
    if not dev:
        c.add("lom", "fail", "address {} not found on any interface".format(lom["ip"]))
        return None
    c.add("lom", "pass", "{} {} mtu {}".format(dev, lom["ip"], all_links[dev].get("mtu")))
    ip = lom["ip"].split("/")[0]
    conn = os.environ.get("SSH_CONNECTION", "").split()
    if len(conn) >= 4:
        r = ip_json("route", "get", conn[0], "from", ip)
        got = r[0].get("dev") if r else ""
        c.add("lom_reply", "pass" if got == dev else "fail",
              "replies to {} from {} leave via {} (expected {})".format(conn[0], ip, got or "?", dev))
    policy = lom.get("policy")
    if policy:
        table, prio = str(policy["table"]), policy["priority"]
        rules = [r for r in ip_json("rule", "show") if r.get("src") == ip and str(r.get("table")) == table]
        problems = []
        if not rules:
            problems.append("no rule 'from {} lookup {}'".format(ip, table))
        elif not any(r.get("priority") == prio for r in rules):
            problems.append("rule priority {} (expected {})".format(
                ",".join(str(r.get("priority")) for r in rules), prio))
        routes = ip_json("route", "show", "table", table)
        if lom.get("gateway") and not any(r.get("dst") == "default" and r.get("gateway") == lom["gateway"]
                                          and r.get("dev") == dev for r in routes):
            problems.append("table {} has no 'default via {} dev {}'".format(table, lom["gateway"], dev))
        c.judge("lom_policy", problems, "from {} lookup {} priority {}".format(ip, table, prio))
    return dev


def check_default(c, default, devs):
    dev = devs.get(default["label"])
    if not dev:
        c.add("default_route", "fail", "{} port not found".format(default["label"]))
        return
    routes = [r for r in ip_json("route", "show", "default") if r.get("dst") == "default"]
    got = ["{} dev {}".format(r.get("gateway", "-"), r.get("dev", "?")) for r in routes]
    want = "{} dev {}".format(default["gateway"], dev)
    c.add("default_route", "pass" if got == [want] else "fail",
          "default via {} (expected only {})".format(", ".join(got) or "none", want))


def check_route(c, route, devs):
    name = "route:{} {}".format(route["label"], route["to"])
    dev = devs.get(route["label"])
    if not dev:
        c.add(name, "fail", "{} port not found".format(route["label"]))
        return
    found = ip_json("route", "show", route["to"])
    ok = any(r.get("gateway") == route["via"] and r.get("dev") == dev for r in found)
    got = ", ".join("via {} dev {}".format(r.get("gateway", "-"), r.get("dev", "?")) for r in found) or "none"
    c.add(name, "pass" if ok else "fail", "{} (expected via {} dev {})".format(got, route["via"], dev))


def check_dns(c, dns, devs):
    name = "dns:" + dns["label"]
    dev = devs.get(dns["label"])
    if not dev:
        c.add(name, "fail", "{} port not found".format(dns["label"]))
        return
    if not shutil.which("resolvectl"):
        c.add(name, "skip", "resolvectl not found")
        return
    rc, out = run(["resolvectl", "dns", dev])
    have = out.split(":", 1)[1].split() if rc == 0 and ":" in out else []
    missing = [s for s in dns["servers"] if s not in have]
    c.add(name, "fail" if missing else "pass",
          "{}: {}{}".format(dev, " ".join(have) or "none", " (missing {})".format(" ".join(missing)) if missing else ""))


def check_ping(c, ping, devs):
    name = "ping:" + ping["name"]
    dev = devs.get(ping["label"])
    if not dev:
        c.add(name, "fail", "{} port not found".format(ping["label"]))
        return
    rc, out = run(["ping", "-c", "3", "-i", "0.2", "-W", "1", "-I", dev, ping["target"]], timeout=20)
    lines = out.splitlines()
    c.add(name, "pass" if rc == 0 else "fail",
          "{} -> {}: {}".format(dev, ping["target"], lines[-2] if len(lines) >= 2 else out))


def main(args):
    global ROOT
    ROOT = args.get("sysfs") or "/sys"
    c = Checks()
    host = socket.gethostname().split(".")[0]
    c.add("hostname", "pass" if host == args["hostname"] else "warn",
          "{}{}".format(host, "" if host == args["hostname"] else " (network.yaml key is {})".format(args["hostname"])))
    all_links = links()
    devs = {"lom": check_lom(c, args["lom"], all_links)}
    for port in args.get("ports", []):
        devs[port["label"]] = check_port(c, port, all_links)
    if args.get("default"):
        check_default(c, args["default"], devs)
    for route in args.get("routes", []):
        check_route(c, route, devs)
    for dns in args.get("dns", []):
        check_dns(c, dns, devs)
    for ping in args.get("pings", []):
        check_ping(c, ping, devs)
    return {"ok": not any(i["result"] == "fail" for i in c.items), "checks": c.items}


if __name__ == "__main__":
    print(json.dumps(main(json.loads(base64.b64decode(os.environ.get("TESTKIT_ARGS", "e30=")).decode()))))
