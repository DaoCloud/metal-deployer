#!/bin/bash
# imex-check.sh - Layered health check of the NVIDIA IMEX service on a
# multi-node NVLink tray (GB300 / GB200 NVL72). Safe to run at any time.
#
#   L0  package / version / unit file                (right after install)
#   L1  local daemon, channel0 device, local READY   (after imex-configure.sh)
#   L2  IMEX domain: every node READY, matrix all C, Domain State UP, ping matrix
#   L3  fabric: GPU probe Complete, Cluster UUID consistent, -sanity, nvidia-smi
#
# Usage:
#   imex-check.sh [--level 0|1|2|3] [--nodes FILE] [--quiet]
#   imex-check.sh --smoke          # no-GPU loopback self-test of the packaging
#                                  # (used by build-iso/test.sh inside QEMU)
#
#   --nodes FILE   also compare md5 of nodes_config.cfg on every listed node
#                  over ssh (best effort, needs root key access)
#
# Exit code: 0 all PASS (WARN/SKIP allowed), 1 at least one FAIL.
set -uo pipefail

IMEX_DIR="${IMEX_DIR:-/etc/nvidia-imex}"
IMEX_CONFIG="${IMEX_CONFIG:-${IMEX_DIR}/config.cfg}"
IMEX_NODES="${IMEX_NODES:-${IMEX_DIR}/nodes_config.cfg}"
IMEX_CHANNEL0=${IMEX_CHANNEL0:-/dev/nvidia-caps-imex-channels/channel0}
CTL_TIMEOUT="${CTL_TIMEOUT:-20}"

MAX_LEVEL=3
NODES_FILE=""
QUIET=0
SMOKE=0

PASS_N=0; FAIL_N=0; WARN_N=0; SKIP_N=0
RESULTS=()

usage() {
    sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --level) MAX_LEVEL="${2:-3}"; shift 2 ;;
        --nodes) NODES_FILE="${2:-}"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        --smoke) SMOKE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

record() {
    # record LEVEL STATUS NAME DETAIL
    local level="$1" status="$2" name="$3" detail="${4:-}"
    case "$status" in
        PASS) PASS_N=$((PASS_N + 1)) ;;
        FAIL) FAIL_N=$((FAIL_N + 1)) ;;
        WARN) WARN_N=$((WARN_N + 1)) ;;
        SKIP) SKIP_N=$((SKIP_N + 1)) ;;
    esac
    RESULTS+=("$(printf '%-3s %-5s %-34s %s' "$level" "$status" "$name" "$detail")")
    [ "$QUIET" -eq 1 ] || printf '  [%s] %-5s %-34s %s\n' "$level" "$status" "$name" "$detail"
}

ctl() {
    timeout "$CTL_TIMEOUT" nvidia-imex-ctl "$@" 2>&1
}

section() {
    [ "$QUIET" -eq 1 ] || echo "== $* =="
}

summary() {
    echo
    echo "================ IMEX check summary ================"
    printf '%s\n' "${RESULTS[@]}"
    echo "----------------------------------------------------"
    echo "PASS=${PASS_N} FAIL=${FAIL_N} WARN=${WARN_N} SKIP=${SKIP_N}"
    if [ "$FAIL_N" -eq 0 ]; then
        echo "RESULT: PASS"
        return 0
    fi
    echo "RESULT: FAIL"
    return 1
}

# --------------------------------------------------------------------------
# --smoke: start a throw-away no-GPU daemon on loopback and query it. Proves
# the package, config parser, gRPC and command service work without GPUs or
# peers. Uses non-default ports so it never clashes with a real daemon.
# --------------------------------------------------------------------------
run_smoke() {
    local tmp pid port cmd_port out rc=1
    section "IMEX packaging smoke test (no GPU, loopback)"
    if ! command -v nvidia-imex >/dev/null 2>&1; then
        record S SKIP "nvidia-imex installed" "binary not found"
        summary; return $?
    fi
    record S PASS "nvidia-imex installed" "$(nvidia-imex --version 2>/dev/null | head -n 1 | tr -s ' ')"
    if command -v nvidia-imex-ctl >/dev/null 2>&1; then
        record S PASS "nvidia-imex-ctl installed" ""
    else
        record S FAIL "nvidia-imex-ctl installed" "missing"
        summary; return $?
    fi
    if [ -f /lib/systemd/system/nvidia-imex.service ] || [ -f /usr/lib/systemd/system/nvidia-imex.service ]; then
        record S PASS "systemd unit present" ""
    else
        record S FAIL "systemd unit present" "nvidia-imex.service missing"
    fi
    if [ -f /etc/systemd/system/nvidia-imex.service.d/10-wait-config.conf ]; then
        record S PASS "drop-in ConditionPathExists" ""
    else
        record S WARN "drop-in ConditionPathExists" "not installed (nvidia_setup.sh)"
    fi
    if grep -qs 'NVreg_CreateImexChannel0=1' /etc/modprobe.d/*.conf; then
        record S PASS "modprobe NVreg_CreateImexChannel0" ""
    else
        record S WARN "modprobe NVreg_CreateImexChannel0" "option not set"
    fi

    port=$(( 50000 + (RANDOM % 400) + 100 ))
    cmd_port=$(( port + 5 ))
    tmp="$(mktemp -d /tmp/imex-smoke.XXXXXX)"
    echo 127.0.0.1 > "$tmp/nodes_config.cfg"
    cat > "$tmp/config.cfg" <<EOF
DAEMONIZE=0
IMEX_NODE_CONFIG_FILE=$tmp/nodes_config.cfg
SERVER_PORT=$port
BIND_INTERFACE_IP=127.0.0.1
IMEX_CMD_ENABLED=1
IMEX_CMD_PORT=$cmd_port
IMEX_CMD_BIND_INTERFACE_IP=127.0.0.1
LOG_FILE_NAME=$tmp/nvidia-imex.log
STATS_FILE_NAME=$tmp/nvidia-imex-stats.log
LOG_LEVEL=4
IMEX_WAIT_FOR_QUORUM=NONE
EOF
    nvidia-imex -nogpu -c "$tmp/config.cfg" >"$tmp/stdout.log" 2>&1 &
    pid=$!
    for _ in $(seq 1 20); do
        sleep 1
        out="$(ctl -c "$tmp/config.cfg" -N 2>/dev/null || true)"
        if echo "$out" | grep -q 'Domain State: UP'; then rc=0; break; fi
    done
    # -nogpu may daemonize; stop every process started with our config file.
    mapfile -t pids < <(pgrep -f "nvidia-imex -nogpu -c $tmp/config.cfg" 2>/dev/null)
    pids+=("$pid")
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
    for _ in $(seq 1 10); do
        alive=0
        for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
        [ "$alive" -eq 0 ] && break
        sleep 1
    done
    for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null || true; done
    wait "$pid" 2>/dev/null || true
    if [ "$rc" -eq 0 ]; then
        record S PASS "nogpu loopback domain UP" "port ${port}"
    else
        record S FAIL "nogpu loopback domain UP" "see ${tmp}/nvidia-imex.log"
        [ "$QUIET" -eq 1 ] || { echo "--- nvidia-imex-ctl -N ---"; echo "$out"; echo "--- log tail ---"; tail -n 20 "$tmp/nvidia-imex.log" "$tmp/stdout.log" 2>/dev/null; }
        summary; return $?
    fi
    rm -rf "$tmp"
    summary
}

if [ "$SMOKE" -eq 1 ]; then
    run_smoke
    exit $?
fi

# --------------------------------------------------------------------------
# L0 package / version
# --------------------------------------------------------------------------
section "L0 package / version"
if command -v nvidia-imex >/dev/null 2>&1; then
    imex_pkg="$(dpkg-query -W -f='${Package} ${Version}\n' 'nvidia-imex*' 2>/dev/null | head -n 1)"
    record L0 PASS "nvidia-imex installed" "${imex_pkg:-$(nvidia-imex --version 2>/dev/null | head -n 1)}"
else
    record L0 FAIL "nvidia-imex installed" "binary not found (apt-get install nvidia-imex-<driver branch>, e.g. nvidia-imex-580)"
    summary; exit $?
fi
command -v nvidia-imex-ctl >/dev/null 2>&1 \
    && record L0 PASS "nvidia-imex-ctl installed" "" \
    || record L0 FAIL "nvidia-imex-ctl installed" "missing"

imex_ver="$(nvidia-imex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n 1)"
drv_ver="$(cat /sys/module/nvidia/version 2>/dev/null)"
if [ -z "$drv_ver" ]; then
    record L0 WARN "driver/imex version match" "nvidia.ko not loaded (imex ${imex_ver:-?})"
elif [ "${imex_ver%%.*}" = "${drv_ver%%.*}" ]; then
    record L0 PASS "driver/imex version match" "imex ${imex_ver} / driver ${drv_ver}"
else
    record L0 FAIL "driver/imex version match" "imex ${imex_ver} vs driver ${drv_ver}"
fi
if [ -f /lib/systemd/system/nvidia-imex.service ] || [ -f /usr/lib/systemd/system/nvidia-imex.service ]; then
    record L0 PASS "systemd unit present" ""
else
    record L0 FAIL "systemd unit present" "nvidia-imex.service missing"
fi
[ -f "$IMEX_CONFIG" ] \
    && record L0 PASS "config.cfg present" "$IMEX_CONFIG" \
    || record L0 FAIL "config.cfg present" "missing $IMEX_CONFIG"
[ "$MAX_LEVEL" -ge 1 ] || { summary; exit $?; }

# --------------------------------------------------------------------------
# L1 local daemon
# --------------------------------------------------------------------------
section "L1 local daemon"
if [ -f "$IMEX_NODES" ]; then
    n_nodes="$(sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "$IMEX_NODES" | wc -l)"
    record L1 PASS "nodes_config.cfg present" "${n_nodes} nodes, md5 $(md5sum "$IMEX_NODES" | cut -c1-8)"
else
    record L1 FAIL "nodes_config.cfg present" "not configured yet: run imex-configure.sh --nodes <rack.list>"
    summary; exit $?
fi
if [ -e "$IMEX_CHANNEL0" ]; then
    record L1 PASS "imex channel0 device" "$IMEX_CHANNEL0"
else
    record L1 FAIL "imex channel0 device" "missing; set NVreg_CreateImexChannel0=1 or nvidia-modprobe -f $IMEX_CHANNEL0"
fi
if systemctl is-active --quiet nvidia-imex.service; then
    record L1 PASS "nvidia-imex.service active" "since $(systemctl show -p ActiveEnterTimestamp --value nvidia-imex.service 2>/dev/null)"
else
    record L1 FAIL "nvidia-imex.service active" "$(systemctl is-active nvidia-imex.service 2>&1); journalctl -u nvidia-imex"
    summary; exit $?
fi
q_out="$(ctl -q)"
if echo "$q_out" | grep -q 'READY'; then
    record L1 PASS "local daemon state" "READY"
else
    record L1 FAIL "local daemon state" "$(echo "$q_out" | head -n 1)"
fi
if [ -n "$NODES_FILE" ] && [ -f "$NODES_FILE" ]; then
    local_md5="$(md5sum "$IMEX_NODES" | cut -d' ' -f1)"
    mismatch=()
    while read -r n; do
        [ -n "$n" ] || continue
        remote="$(timeout 10 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@$n" md5sum "$IMEX_NODES" 2>/dev/null | cut -d' ' -f1)"
        [ "$remote" = "$local_md5" ] || mismatch+=("$n")
    done < <(sed -e 's/#.*$//' -e 's/[[:space:]]//g' -e '/^$/d' "$NODES_FILE")
    if [ "${#mismatch[@]}" -eq 0 ]; then
        record L1 PASS "nodes_config.cfg identical on all" ""
    else
        record L1 FAIL "nodes_config.cfg identical on all" "differs/unreachable: ${mismatch[*]}"
    fi
fi
[ "$MAX_LEVEL" -ge 2 ] || { summary; exit $?; }

# --------------------------------------------------------------------------
# L2 domain connectivity
# --------------------------------------------------------------------------
section "L2 IMEX domain"
N_out="$(ctl -N -H)"
node_lines="$(echo "$N_out" | grep -E '^Node #')"
total="$(echo "$node_lines" | grep -c . || true)"
ready="$(echo "$node_lines" | grep -c 'READY' || true)"
if [ "$total" -gt 0 ] && [ "$ready" -eq "$total" ]; then
    record L2 PASS "all nodes READY" "${ready}/${total}"
else
    record L2 FAIL "all nodes READY" "${ready}/${total}; $(echo "$node_lines" | grep -v READY | awk '{print $3}' | tr '\n' ' ')"
fi
# Matrix rows: "<idx> <sym> <sym> ..." after the "Nodes From\To" header.
matrix="$(echo "$N_out" | awk '/Nodes From/{m=1; next} /Domain State/{m=0} m && NF>1 {$1=""; print}')"
bad="$(echo "$matrix" | tr ' ' '\n' | grep -v '^$' | grep -vc '^C$' || true)"
if [ -n "$matrix" ] && [ "$bad" -eq 0 ]; then
    record L2 PASS "connectivity matrix all C" "${total}x${total}"
else
    syms="$(echo "$matrix" | tr ' ' '\n' | grep -v '^$' | grep -v '^C$' | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')"
    record L2 FAIL "connectivity matrix all C" "non-C cells: ${syms:-matrix not found}"
fi
state="$(echo "$N_out" | sed -n 's/^Domain State: *//p' | tail -n 1)"
if [ "$state" = "UP" ]; then
    record L2 PASS "domain state" "UP"
else
    record L2 FAIL "domain state" "${state:-unknown}"
fi
ping_out="$(ctl -ping)"
if echo "$ping_out" | grep -q 'Ping Stats'; then
    if echo "$ping_out" | awk '/Ping Stats/{p=1} p' | grep -qE '(^|[[:space:]])(X|T)([[:space:]]|$)'; then
        record L2 FAIL "ping matrix" "unreachable (X) or timeout (T) entries"
    else
        record L2 PASS "ping matrix" "no X/T"
    fi
else
    record L2 SKIP "ping matrix" "-ping not supported by this nvidia-imex-ctl"
fi
[ "$MAX_LEVEL" -ge 3 ] || { summary; exit $?; }

# --------------------------------------------------------------------------
# L3 fabric
# --------------------------------------------------------------------------
section "L3 fabric"
D_out="$(ctl -D)"
if echo "$D_out" | grep -qi 'unknown command line flag'; then
    D_out=""
    record L3 SKIP "nvidia-imex-ctl -D GPU details" "not supported by this nvidia-imex-ctl version"
fi
probe_total="$(echo "$D_out" | grep -c 'Probe State:' || true)"
probe_ok="$(echo "$D_out" | grep -c 'Probe State: *Complete' || true)"
if [ "$probe_total" -gt 0 ] && [ "$probe_ok" -eq "$probe_total" ]; then
    record L3 PASS "GPU fabric probe Complete" "${probe_ok}/${probe_total} GPUs"
elif [ "$probe_total" -eq 0 ] && [ -n "$D_out" ]; then
    record L3 WARN "GPU fabric probe Complete" "no GPU details (IMEX_CMD_ALLOW_SENSITIVE_INFO=0)"
elif [ "$probe_total" -gt 0 ]; then
    record L3 FAIL "GPU fabric probe Complete" "${probe_ok}/${probe_total}; NMX-C / NVLink cabling issue, not node side"
fi
uuids="$(echo "$D_out" | sed -n 's/.*Cluster UUID: *//p' | tr -d ' ' | sort -u)"
uuid_n="$(echo "$uuids" | grep -c . || true)"
if [ "$uuid_n" -eq 1 ] && ! echo "$uuids" | grep -qE '^0+$'; then
    record L3 PASS "Cluster UUID consistent" "$uuids"
elif [ "$uuid_n" -gt 1 ]; then
    record L3 FAIL "Cluster UUID consistent" "$(echo "$uuids" | tr '\n' ' ')"
fi
health_bad="$(echo "$D_out" | grep 'Fabric Health Summary' | grep -vc 'Healthy' || true)"
if echo "$D_out" | grep -q 'Fabric Health Summary'; then
    [ "$health_bad" -eq 0 ] \
        && record L3 PASS "Fabric Health Healthy" "" \
        || record L3 FAIL "Fabric Health Healthy" "${health_bad} GPU(s) degraded"
fi
san_out="$(ctl -sanity)"
if echo "$san_out" | grep -qiE 'unknown|invalid option|usage:'; then
    record L3 SKIP "nvidia-imex-ctl -sanity" "not supported by this version"
elif echo "$san_out" | grep -qiE 'FAIL|mismatch|error'; then
    record L3 FAIL "nvidia-imex-ctl -sanity" "$(echo "$san_out" | grep -iE 'FAIL|mismatch|error' | head -n 1)"
else
    record L3 PASS "nvidia-imex-ctl -sanity" ""
fi
if command -v nvidia-smi >/dev/null 2>&1; then
    smi="$(timeout 30 nvidia-smi -q 2>/dev/null)"
    f_total="$(echo "$smi" | grep -c 'Fabric' || true)"
    f_state="$(echo "$smi" | awk -F': *' '/^ *Fabric/{f=1; next} f && /State/{print $2; f=0}')"
    f_bad="$(echo "$f_state" | grep -vc 'Completed' || true)"
    if [ "$f_total" -eq 0 ]; then
        record L3 SKIP "nvidia-smi Fabric State" "no Fabric section (not a multi-node NVLink GPU)"
    elif [ "$f_bad" -eq 0 ]; then
        record L3 PASS "nvidia-smi Fabric State" "Completed on $(echo "$f_state" | grep -c . ) GPUs"
    else
        record L3 FAIL "nvidia-smi Fabric State" "$(echo "$f_state" | sort | uniq -c | tr '\n' ' ')"
    fi
    errs="$(ctl --get_error_counters | grep -iE 'total' | head -n 3 | tr -s ' ' | tr '\n' ';')"
    [ -n "$errs" ] && record L3 PASS "error counters" "$errs"
else
    record L3 SKIP "nvidia-smi Fabric State" "nvidia-smi missing"
fi

summary
