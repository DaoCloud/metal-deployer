"""Run on a host (python3, stdlib only): netplan inspection, guarded apply, confirm, verify.

mtest sends this file over ssh stdin; MTEST_ARGS (base64 JSON) holds {"action": ..., ...}.
Prints one JSON object.

apply never leaves a host without a way back: before "netplan apply" it copies
/etc/netplan to a backup directory and starts a systemd timer that restores the backup
and re-applies it after rollback_seconds. "confirm" stops that timer once mtest has
reconnected and checked the host.
"""

import base64
import glob
import json
import os
import shutil
import subprocess

NETPLAN = "/etc/netplan"
OWN = os.path.join(NETPLAN, "60-mtest.yaml")
STATE = "/var/lib/mtest"
BACKUP = os.path.join(STATE, "netplan-backup")
# Whoever creates this directory first (rollback or confirm) wins; the other backs off.
CLAIM = os.path.join(STATE, "rollback.claim")
UNIT = "mtest-net-rollback"


def run(cmd, timeout=120, shell=False):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, shell=shell)
        return p.returncode, p.stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def rollback_active():
    rc, _ = run(["systemctl", "is-active", "--quiet", UNIT + ".timer"])
    return rc == 0


def inspect(args):
    files = {}
    for path in sorted(glob.glob(os.path.join(NETPLAN, "*.yaml"))):
        if path != OWN:
            files[os.path.basename(path)] = read(path, "")
    return {"files": files, "own": read(OWN), "rollback_active": rollback_active(),
            "netplan": shutil.which("netplan") is not None, "systemd_run": shutil.which("systemd-run") is not None}


def restore_cmd(apply=True):
    """Shell that puts the backup back. It never empties /etc/netplan: files are copied
    first, and only files that were not in the backup (such as a new 60-mtest.yaml) are removed."""
    return ("mkdir {c} 2>/dev/null || exit 0; ls {b}/*.yaml >/dev/null 2>&1 || exit 1; "
            "cp -p {b}/*.yaml {n}/ && for f in {n}/*.yaml; do [ -e {b}/$(basename $f) ] || rm -f $f; done; "
            "{last}").format(c=CLAIM, n=NETPLAN, b=BACKUP, last="netplan apply" if apply else "netplan generate")


def apply(args):
    if not shutil.which("netplan") or not shutil.which("systemd-run"):
        return {"ok": False, "error": "netplan and systemd-run are required"}
    if rollback_active():
        return {"ok": False, "error": "a previous mtest network change is still waiting for confirmation"}
    shutil.rmtree(BACKUP, ignore_errors=True)
    shutil.rmtree(CLAIM, ignore_errors=True)
    os.makedirs(BACKUP, mode=0o700)
    for path in glob.glob(os.path.join(NETPLAN, "*.yaml")):
        shutil.copy2(path, BACKUP)
    tmp = OWN + ".new"
    with open(tmp, "w") as f:
        f.write(args["content"])
    os.chmod(tmp, 0o600)
    os.replace(tmp, OWN)
    rc, out = run(["netplan", "generate"])
    if rc != 0:
        run(restore_cmd(apply=False), shell=True)
        return {"ok": False, "error": "netplan generate failed: " + out[-500:]}
    run(["systemctl", "reset-failed", UNIT + ".service", UNIT + ".timer"])
    rc, out = run(["systemd-run", "--unit=" + UNIT, "--on-active={}s".format(int(args["rollback_seconds"])),
                   "/bin/sh", "-c", restore_cmd()])
    if rc != 0:
        run(restore_cmd(apply=False), shell=True)
        return {"ok": False, "error": "cannot start the rollback timer: " + out[-300:]}
    # Apply in the background so this ssh session can end before routes change.
    subprocess.Popen(["/bin/sh", "-c", "sleep 1; netplan apply > {}/apply.log 2>&1".format(STATE)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"ok": True}


def confirm(args):
    try:
        os.mkdir(CLAIM)
    except FileExistsError:
        return {"ok": False, "rolled_back": True}
    run(["systemctl", "stop", UNIT + ".timer"])
    run(["systemctl", "reset-failed", UNIT + ".service", UNIT + ".timer"])
    shutil.rmtree(BACKUP, ignore_errors=True)
    return {"ok": True, "apply_log": read(os.path.join(STATE, "apply.log"), "")[-2000:]}


def set_link_type(args):
    out = {}
    for pci in args["pcis"]:
        rc, text = run(["mlxconfig", "-y", "-d", pci, "set", "LINK_TYPE_P1={}".format(args.get("value", 1))],
                       timeout=300)
        out[pci] = {"ok": rc == 0, "output": text[-500:]}
    return {"ok": all(v["ok"] for v in out.values()), "devices": out}


def reboot(args):
    subprocess.Popen(["/bin/sh", "-c", "sleep 2; systemctl reboot"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True}


def check(results, name, ok, detail=""):
    results.append({"name": name, "result": "pass" if ok else "fail", "detail": detail})


def ip_json(*args):
    rc, out = run(["ip", "-j"] + list(args))
    if rc != 0:
        return []
    try:
        return json.loads(out or "[]")
    except ValueError:
        return []


def verify(args):
    res = []
    sysfs = args.get("sysfs") or "/sys"
    ctl = args.get("control_ip")
    if ctl:
        r = ip_json("route", "get", ctl)
        dev = r[0].get("dev") if r else ""
        check(res, "route_control", dev == args["lom_if"],
              "{} via {} (expected {})".format(ctl, dev or "?", args["lom_if"]))
    if args.get("default_if"):
        r = [x for x in ip_json("route", "show", "default") if x.get("dst") == "default"]
        devs = [x.get("dev") for x in r]
        check(res, "default_route", bool(devs) and devs[0] == args["default_if"] and len(set(devs)) == 1,
              "default via {} (expected only {})".format(",".join(d or "?" for d in devs) or "none",
                                                          args["default_if"]))
    for iface in args.get("ifaces", []):
        link = ip_json("addr", "show", "dev", iface["name"])
        if not link:
            check(res, "iface:" + iface["label"], False, "{} not found".format(iface["name"]))
            continue
        link = link[0]
        addrs = ["{}/{}".format(a["local"], a["prefixlen"]) for a in link.get("addr_info", []) if a.get("local")]
        mtu = link.get("mtu")
        problems = []
        if iface.get("addr") and iface["addr"] not in addrs:
            problems.append("address {} missing (has {})".format(iface["addr"], ",".join(addrs) or "none"))
        if iface.get("mtu") and mtu != iface["mtu"]:
            problems.append("mtu {} (expected {})".format(mtu, iface["mtu"]))
        if link.get("operstate") == "DOWN":
            problems.append("link down")
        check(res, "iface:" + iface["label"], not problems,
              "; ".join(problems) or "{} {} mtu {}".format(iface["name"], iface.get("addr", ""), mtu))
    for ib in args.get("ib", []):
        base = os.path.join(sysfs, "class/infiniband", ib["ibdev"], "ports/1")
        state = (read(os.path.join(base, "state"), "") or "").split(":")[-1].strip()
        rate_s = (read(os.path.join(base, "rate"), "") or "").strip()
        try:
            rate = float(rate_s.split()[0])
        except (ValueError, IndexError):
            rate = 0.0
        ok = state == "ACTIVE" and (not ib.get("min_rate") or rate >= ib["min_rate"])
        check(res, "ib:" + ib["label"], ok, "{} {} {}".format(ib["ibdev"], state or "?", rate_s or "?"))
    for p in args.get("pings", []):
        rc, out = run(["ping", "-c", "3", "-i", "0.2", "-W", "1", "-I", p["iface"], p["target"]], timeout=20)
        tail = out.splitlines()[-2] if out.count("\n") >= 1 else out
        check(res, "ping:" + p["label"], rc == 0, "{} -> {}: {}".format(p["iface"], p["target"], tail))
    return {"ok": all(r["result"] == "pass" for r in res), "checks": res}


ACTIONS = {"inspect": inspect, "apply": apply, "confirm": confirm, "verify": verify,
           "set_link_type": set_link_type, "reboot": reboot}

if __name__ == "__main__":
    a = json.loads(base64.b64decode(os.environ.get("MTEST_ARGS", "e30=")).decode())
    print(json.dumps(ACTIONS[a["action"]](a)))
