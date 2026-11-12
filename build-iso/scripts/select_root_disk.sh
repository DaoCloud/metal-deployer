#!/bin/bash
# select_root_disk.sh - choose and guard the autoinstall root disk.
#
# Runs as an autoinstall early-command in the live installer, before subiquity
# probes disks:
#   early-commands:
#     - bash /cdrom/resource/scripts/select_root_disk.sh
#
# Subiquity re-reads /autoinstall.yaml after early-commands, so this script can
# change storage.layout.match without rebuilding the ISO. With NICo, add the
# options below to the kernel parameters of the Operating System definition.
#
# Kernel command line options:
#   md.root_disk=<spec>       Replace storage.layout.match. <spec> is one of:
#                               smallest | largest
#                               model:<glob> | serial:<glob> | id_path:<glob> | path:<glob>
#                             model/serial/id_path match udev ID_MODEL/ID_SERIAL/ID_PATH,
#                             path matches the device path (e.g. path:/dev/nvme0n1).
#   md.root_disk_check=0      Skip the safety check (selection is still logged).
#
# Safety check (default on). Before partitioning, the script predicts which
# disk the match selects and stops the installation when:
#   - no disk matches;
#   - the selected disk is USB or removable;
#   - with size: smallest|largest, several disks share the selected size but
#     have different models (for example an M.2 boot drive and an E1.S data
#     drive of the same capacity), so the choice would be arbitrary.
#
# The log is written to /var/log/installer/, which subiquity copies into the
# installed system.
set -uo pipefail

CMDLINE_FILE="${CMDLINE_FILE:-/proc/cmdline}"
AUTOINSTALL_FILE="${AUTOINSTALL_FILE:-/autoinstall.yaml}"
LOG_FILE="${LOG_FILE:-/var/log/installer/metal-deployer-root-disk.log}"
# Test hook: JSON list of disks instead of probing lsblk/udev.
ROOT_DISK_INVENTORY="${ROOT_DISK_INVENTORY:-}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

log() {
    echo "[select_root_disk] $*" | tee -a "$LOG_FILE" >&2
}

override_requested() {
    tr ' ' '\n' < "$CMDLINE_FILE" | grep -q '^md\.root_disk='
}

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
    if override_requested; then
        log "ERROR: python3 with PyYAML is not available; cannot apply md.root_disk."
        exit 1
    fi
    log "WARNING: python3 with PyYAML is not available; root disk check skipped."
    exit 0
fi

CMDLINE_FILE="$CMDLINE_FILE" AUTOINSTALL_FILE="$AUTOINSTALL_FILE" \
ROOT_DISK_INVENTORY="$ROOT_DISK_INVENTORY" \
python3 - 2>&1 <<'PY' | tee -a "$LOG_FILE" >&2
import fnmatch
import json
import os
import re
import subprocess
import sys

import yaml

PREFIX = "[select_root_disk]"
HINT = ("Fix the disk layout, or set md.root_disk=<spec> (or md.root_disk_check=0) "
        "on the kernel command line, then reinstall.")


def log(msg):
    print(f"{PREFIX} {msg}", flush=True)


def die(msg):
    log(f"ERROR: {msg}")
    log(HINT)
    sys.exit(1)


def natural_key(text):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def human(size):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.2f}{unit}"
        size /= 1000.0
    return str(size)


def run_json(cmd):
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def udev_props(path):
    try:
        out = subprocess.run(["udevadm", "info", "-q", "property", "-n", path],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def has_mountpoint(path):
    # lsblk on a disk lists the disk and all its descendants (partitions, LVM, RAID).
    try:
        out = subprocess.run(["lsblk", "-nr", "-o", "MOUNTPOINTS", path],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(line.strip() for line in out.splitlines())


def probe_disks():
    inventory = os.environ.get("ROOT_DISK_INVENTORY")
    if inventory:
        with open(inventory) as fh:
            return json.load(fh)
    top = run_json(["lsblk", "-J", "-b", "-d", "-o", "NAME,PATH,TYPE,SIZE,TRAN,RM,MODEL,SERIAL"])
    disks = []
    for dev in top.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        props = udev_props(dev["path"])
        disks.append({
            "path": dev["path"],
            "size": int(dev.get("size") or 0),
            "tran": dev.get("tran") or "",
            "rm": str(dev.get("rm")).lower() in ("1", "true"),
            "model": (dev.get("model") or "").strip(),
            "serial": (dev.get("serial") or "").strip(),
            "id_model": props.get("ID_MODEL", ""),
            "id_serial": props.get("ID_SERIAL", ""),
            "id_path": props.get("ID_PATH", ""),
            "in_use": has_mountpoint(dev["path"]),
        })
    return disks


def parse_spec(spec):
    if spec in ("smallest", "largest"):
        return {"size": spec}
    key, sep, value = spec.partition(":")
    if sep and value and key in ("model", "serial", "id_path", "path"):
        return {key: value}
    die(f"invalid md.root_disk={spec!r}; use smallest, largest, model:<glob>, "
        "serial:<glob>, id_path:<glob> or path:<glob>")


args = {}
with open(os.environ["CMDLINE_FILE"]) as fh:
    for token in fh.read().split():
        if "=" in token:
            key, value = token.split("=", 1)
            args[key] = value.strip("\"'")
spec = args.get("md.root_disk")
check = args.get("md.root_disk_check", "1") != "0"

ai_path = os.environ["AUTOINSTALL_FILE"]
try:
    with open(ai_path) as fh:
        doc = yaml.safe_load(fh) or {}
except FileNotFoundError:
    die(f"{ai_path} not found")
root = doc["autoinstall"] if isinstance(doc.get("autoinstall"), dict) else doc
storage = root.get("storage") or {}

if spec:
    if "config" in storage:
        die("storage uses an explicit 'config' section; md.root_disk only supports storage.layout")
    new_match = parse_spec(spec)
    layout = root.setdefault("storage", {}).setdefault("layout", {})
    layout.setdefault("name", "direct")
    old_match = layout.get("match")
    layout["match"] = new_match
    with open(ai_path, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, default_flow_style=False)
    log(f"md.root_disk={spec}: storage.layout.match {old_match} -> {new_match}")
    storage = root["storage"]

match = (storage.get("layout") or {}).get("match")
if "config" in storage or not isinstance(match, dict) or not match:
    log(f"no storage.layout.match to evaluate ({'config' if 'config' in storage else match}); check skipped")
    sys.exit(0)

disks = sorted(probe_disks(), key=lambda d: natural_key(d["path"]))
log(f"storage.layout.match = {match}")
log("disks:")
for d in disks:
    flags = [f for f, on in (("usb", d["tran"] == "usb"), ("removable", d["rm"]),
                             ("in-use", d["in_use"])) if on]
    log(f"  {d['path']:<16} {human(d['size']):>10}  tran={d['tran'] or '-'}  "
        f"model={d['model'] or '-'}  serial={d['serial'] or '-'}  "
        f"id_path={d['id_path'] or '-'}  {' '.join(flags)}")

candidates = [d for d in disks if d["size"] > 0]
ties = []
if "size" in match:
    candidates = [d for d in candidates if not d["in_use"]]
    candidates.sort(key=lambda d: d["size"], reverse=match["size"] == "largest")
    if candidates:
        ties = [d for d in candidates if d["size"] == candidates[0]["size"]]
else:
    fields = {"model": "id_model", "serial": "id_serial", "id_path": "id_path", "path": "path"}
    unsupported = [k for k in match if k not in fields]
    if unsupported:
        log(f"match keys {unsupported} are not evaluated by this script; check skipped")
        sys.exit(0)
    candidates = [d for d in candidates
                  if all(fnmatch.fnmatchcase(d[fields[k]], str(v)) for k, v in match.items())]

if not candidates:
    if check:
        die(f"no disk matches {match}")
    log(f"WARNING: no disk matches {match}")
    sys.exit(0)

chosen = candidates[0]
log(f"predicted root disk: {chosen['path']} ({human(chosen['size'])}, model={chosen['model'] or '-'}, "
    f"serial={chosen['serial'] or '-'})")

problems = []
if chosen["tran"] == "usb" or chosen["rm"]:
    problems.append(f"{chosen['path']} is a USB or removable disk")
if len({d["model"] for d in ties}) > 1:
    names = ", ".join(f"{d['path']} ({d['model'] or '-'})" for d in ties)
    problems.append(f"size: {match['size']} is ambiguous; same size, different models: {names}")

if problems:
    for p in problems:
        if check:
            log(f"ERROR: {p}")
        else:
            log(f"WARNING: {p} (md.root_disk_check=0)")
    if check:
        log(HINT)
        sys.exit(1)
log("root disk check passed" if check else "root disk check disabled (md.root_disk_check=0)")
PY
rc=${PIPESTATUS[0]}
exit "$rc"
