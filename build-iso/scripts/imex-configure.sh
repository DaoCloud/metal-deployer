#!/bin/bash
# imex-configure.sh - Day-2 configuration of the NVIDIA IMEX daemon on a
# multi-node NVLink compute tray (GB300 / GB200 NVL72).
#
# The ISO installs nvidia-imex but cannot start it: the daemon needs the
# management IPs of every tray in the NVLink domain. Run this on each of the
# 18 trays of a rack with the same node list to write nodes_config.cfg, pin
# config.cfg to the right interface/port, and (re)start the service.
#
# Usage:
#   imex-configure.sh --nodes <file> [--bind-ip <ip>] [--port <n>]
#                     [--cmd-port <n>] [--expect <n>] [--no-restart] [--force]
#
#   --nodes FILE     one IP/hostname per line, every tray in the domain, same
#                    file on every tray (comments and blank lines are ignored)
#   --bind-ip IP     BIND_INTERFACE_IP; default: the entry of FILE that matches
#                    one of this host's addresses
#   --port N         SERVER_PORT (gRPC between daemons), default 50000
#   --cmd-port N     IMEX_CMD_PORT (nvidia-imex-ctl), default 50005
#   --expect N       expected node count, default 18 (NVL72); mismatch = warning
#   --no-restart     write configuration only
#   --force          proceed even if this host is not in FILE
set -euo pipefail

IMEX_DIR="${IMEX_DIR:-/etc/nvidia-imex}"
IMEX_CONFIG="${IMEX_CONFIG:-${IMEX_DIR}/config.cfg}"
IMEX_NODES="${IMEX_NODES:-${IMEX_DIR}/nodes_config.cfg}"
IMEX_CHANNEL0=/dev/nvidia-caps-imex-channels/channel0
LOG_DIR="${LOG_DIR:-/var/log/metal-deployer}"

NODES_FILE=""
BIND_IP=""
SERVER_PORT=50000
CMD_PORT=50005
EXPECT_NODES=18
RESTART=1
FORCE=0

log() {
    echo "[imex-configure] $*"
}

die() {
    echo "[imex-configure] ERROR: $*" >&2
    exit 1
}

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --nodes) NODES_FILE="${2:-}"; shift 2 ;;
        --bind-ip) BIND_IP="${2:-}"; shift 2 ;;
        --port) SERVER_PORT="${2:-}"; shift 2 ;;
        --cmd-port) CMD_PORT="${2:-}"; shift 2 ;;
        --expect) EXPECT_NODES="${2:-}"; shift 2 ;;
        --no-restart) RESTART=0; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -n "$NODES_FILE" ] || { echo "--nodes is required" >&2; usage 1; }
[ -f "$NODES_FILE" ] || die "node list not found: $NODES_FILE"
command -v nvidia-imex >/dev/null 2>&1 || die "nvidia-imex is not installed"
[ -f "$IMEX_CONFIG" ] || die "missing $IMEX_CONFIG (package default); reinstall nvidia-imex"

# Version guard: the daemon refuses to start against a mismatched kernel driver.
imex_version="$(nvidia-imex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1 || true)"
drv_version="$(cat /sys/module/nvidia/version 2>/dev/null || true)"
if [ -n "$imex_version" ] && [ -n "$drv_version" ] && [ "${imex_version%%.*}" != "${drv_version%%.*}" ]; then
    die "nvidia-imex ${imex_version} does not match loaded driver ${drv_version}"
fi

mapfile -t NODES < <(sed -e 's/#.*$//' -e 's/[[:space:]]//g' -e '/^$/d' "$NODES_FILE")
[ "${#NODES[@]}" -gt 0 ] || die "node list is empty"
if [ "${#NODES[@]}" -ne "$EXPECT_NODES" ]; then
    log "WARNING: node list has ${#NODES[@]} entries, expected ${EXPECT_NODES} (NVL72 rack)."
fi
if [ "$(printf '%s\n' "${NODES[@]}" | sort | uniq -d | wc -l)" -ne 0 ]; then
    die "node list contains duplicates"
fi

mapfile -t LOCAL_IPS < <(ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}')
LOCAL_HOST="$(hostname)"
LOCAL_FQDN="$(hostname -f 2>/dev/null || true)"

local_entry=""
for n in "${NODES[@]}"; do
    for ip in "${LOCAL_IPS[@]}"; do
        [ "$n" = "$ip" ] && { local_entry="$n"; break 2; }
    done
    [ "$n" = "$LOCAL_HOST" ] || [ "$n" = "$LOCAL_FQDN" ] && { local_entry="$n"; break; }
done

if [ -z "$local_entry" ]; then
    if [ "$FORCE" -eq 1 ]; then
        log "WARNING: this host is not in the node list (continuing because --force)."
    else
        die "this host (${LOCAL_HOST}, ${LOCAL_IPS[*]:-no-ipv4}) is not in ${NODES_FILE}; fix the list or pass --force"
    fi
fi

if [ -z "$BIND_IP" ] && [ -n "$local_entry" ]; then
    if [[ "$local_entry" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        BIND_IP="$local_entry"
    else
        BIND_IP="$(getent ahostsv4 "$local_entry" | awk 'NR==1{print $1}' || true)"
    fi
fi

set_cfg() {
    local key="$1" value="$2"
    if grep -qE "^[#[:space:]]*${key}=" "$IMEX_CONFIG"; then
        sed -i -E "s|^[#[:space:]]*${key}=.*|${key}=${value}|" "$IMEX_CONFIG"
    else
        printf '%s=%s\n' "$key" "$value" >> "$IMEX_CONFIG"
    fi
}

mkdir -p "$IMEX_DIR" "$LOG_DIR"
if [ -f "$IMEX_CONFIG" ] && [ ! -f "${IMEX_CONFIG}.orig" ]; then
    cp -a "$IMEX_CONFIG" "${IMEX_CONFIG}.orig"
fi

printf '%s\n' "${NODES[@]}" > "${IMEX_NODES}.tmp"
install -m 0644 "${IMEX_NODES}.tmp" "$IMEX_NODES"
rm -f "${IMEX_NODES}.tmp"
log "wrote ${IMEX_NODES} (${#NODES[@]} nodes, md5 $(md5sum "$IMEX_NODES" | cut -d' ' -f1))"

set_cfg IMEX_NODE_CONFIG_FILE "$IMEX_NODES"
set_cfg SERVER_PORT "$SERVER_PORT"
set_cfg BIND_INTERFACE_IP "$BIND_IP"
# Command service must be reachable from the other trays so that
# `nvidia-imex-ctl -N` can build the full domain matrix from any node.
set_cfg IMEX_CMD_ENABLED 1
set_cfg IMEX_CMD_PORT "$CMD_PORT"
set_cfg IMEX_CMD_BIND_INTERFACE_IP "$BIND_IP"
set_cfg LOG_FILE_NAME /var/log/nvidia-imex.log
log "updated ${IMEX_CONFIG}: SERVER_PORT=${SERVER_PORT} BIND_INTERFACE_IP=${BIND_IP:-<all>} IMEX_CMD_PORT=${CMD_PORT}"

if [ ! -e "$IMEX_CHANNEL0" ]; then
    if command -v nvidia-modprobe >/dev/null 2>&1; then
        nvidia-modprobe -f "$IMEX_CHANNEL0" 2>/dev/null || true
    fi
    if [ -e "$IMEX_CHANNEL0" ]; then
        log "created ${IMEX_CHANNEL0}"
    else
        log "WARNING: ${IMEX_CHANNEL0} missing; ensure 'options nvidia NVreg_CreateImexChannel0=1' and reboot, or run nvidia-modprobe -f ${IMEX_CHANNEL0}"
    fi
fi

systemctl daemon-reload
systemctl enable nvidia-imex.service >/dev/null 2>&1 || true

if [ "$RESTART" -eq 0 ]; then
    log "configuration written; service not restarted (--no-restart)."
    exit 0
fi

log "restarting nvidia-imex ..."
systemctl restart nvidia-imex.service
sleep 3
if ! systemctl is-active --quiet nvidia-imex.service; then
    journalctl -u nvidia-imex -n 30 --no-pager || true
    die "nvidia-imex failed to start; see journalctl -u nvidia-imex and /var/log/nvidia-imex.log"
fi

# Local daemon status; peers may still be coming up, so only the local READY
# state is required here. Run imex-check.sh once every tray is configured.
for _ in $(seq 1 10); do
    if out="$(nvidia-imex-ctl -q 2>/dev/null)" && echo "$out" | grep -q 'READY'; then
        log "local daemon READY"
        echo "$out"
        log "done. After all ${EXPECT_NODES} trays are configured, run: /opt/resource/scripts/imex-check.sh"
        exit 0
    fi
    sleep 3
done
log "WARNING: local daemon not READY yet:"
nvidia-imex-ctl -q 2>&1 || true
log "it keeps retrying peers in the background; re-check with: nvidia-imex-ctl -N -H"
