#!/bin/bash
# imex-bench.sh - one-shot functional / performance test of cross-node NVLink
# (IMEX) on a GB300 / GB200 NVL72 rack. Run from any configured tray.
#
#   T0  imex-check.sh L0-L3 on this node (abort on FAIL unless --skip-check)
#   T1  imex_fabric_ipc_test: fabric handle export/import + data verification
#       between two trays (proves IMEX itself, independent of NCCL)
#   T2  nvbandwidth multinode_* device_to_device copy bandwidth (needs the
#       -DMULTINODE=1 build produced by install_test_tools.sh when IMEX+MPI exist)
#   T3  nccl-tests sendrecv / alltoall / all_reduce with NCCL_MNNVL_ENABLE=1,
#       then all_reduce with NCCL_MNNVL_ENABLE=0 as the InfiniBand baseline
#
# Usage:
#   imex-bench.sh --nodes <file> [--gpus-per-node N] [--only t1,t2,t3]
#                 [--max-bytes 8G] [--skip-check] [--mpi-args "..."]
#
# Requirements: passwordless root ssh between trays (MPI), identical tool
# paths on every tray (ISO default), imex-configure.sh already applied.
#
# Report: /var/log/metal-deployer/imex-bench-<timestamp>.log (summary at end).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="${TOOLS_DIR:-/opt/gpu-rdma-test-tools/bin}"
LOG_DIR="${LOG_DIR:-/var/log/metal-deployer}"
TS="$(date +%Y%m%d-%H%M%S)"
REPORT="${LOG_DIR}/imex-bench-${TS}.log"
RAW_DIR="${LOG_DIR}/imex-bench-${TS}"

NODES_FILE=""
GPUS_PER_NODE=""
ONLY="t1,t2,t3"
MAX_BYTES="8G"
SKIP_CHECK=0
EXTRA_MPI_ARGS=""

usage() {
    sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --nodes) NODES_FILE="${2:-}"; shift 2 ;;
        --gpus-per-node) GPUS_PER_NODE="${2:-}"; shift 2 ;;
        --only) ONLY="${2:-}"; shift 2 ;;
        --max-bytes) MAX_BYTES="${2:-}"; shift 2 ;;
        --skip-check) SKIP_CHECK=1; shift ;;
        --mpi-args) EXTRA_MPI_ARGS="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown argument: $1" >&2; usage 1 ;;
    esac
done

[ -n "$NODES_FILE" ] && [ -f "$NODES_FILE" ] || { echo "--nodes <file> is required" >&2; usage 1; }
mkdir -p "$LOG_DIR" "$RAW_DIR"
exec > >(tee -a "$REPORT") 2>&1

log() { echo "[imex-bench $(date '+%T')] $*"; }
want() { [[ ",${ONLY}," == *",$1,"* ]]; }

RESULTS=()
FAIL_N=0
result() {
    # result NAME STATUS DETAIL
    [ "$2" = "FAIL" ] && FAIL_N=$((FAIL_N + 1))
    RESULTS+=("$(printf '%-28s %-5s %s' "$1" "$2" "${3:-}")")
    log "$1: $2 ${3:-}"
}

mapfile -t NODES < <(sed -e 's/#.*$//' -e 's/[[:space:]]//g' -e '/^$/d' "$NODES_FILE")
NN="${#NODES[@]}"
[ "$NN" -ge 2 ] || { echo "need at least 2 nodes in $NODES_FILE" >&2; exit 1; }

# MPI environment: prefer HPC-X when present, else system OpenMPI.
if [ -f /etc/profile.d/hpcx.sh ]; then
    # shellcheck disable=SC1091
    . /etc/profile.d/hpcx.sh >/dev/null 2>&1 || true
    command -v hpcx_load >/dev/null 2>&1 && hpcx_load >/dev/null 2>&1 || true
fi
export PATH="${TOOLS_DIR}:/usr/local/cuda/bin:${PATH}"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
command -v mpirun >/dev/null 2>&1 || { echo "mpirun not found (install openmpi-bin or HPC-X)" >&2; exit 1; }

if [ -z "$GPUS_PER_NODE" ]; then
    GPUS_PER_NODE="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true)"
    [ "$GPUS_PER_NODE" -gt 0 ] 2>/dev/null || GPUS_PER_NODE=4
fi
NP=$((NN * GPUS_PER_NODE))
HOSTS_ALL="$(for n in "${NODES[@]}"; do printf '%s:%s,' "$n" "$GPUS_PER_NODE"; done | sed 's/,$//')"
HOSTS_PAIR="${NODES[0]}:${GPUS_PER_NODE},${NODES[1]}:${GPUS_PER_NODE}"

MPI_COMMON=(--allow-run-as-root --bind-to none --map-by slot
    -x PATH -x LD_LIBRARY_PATH
    -mca plm_rsh_args "-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
# shellcheck disable=SC2206
[ -n "$EXTRA_MPI_ARGS" ] && MPI_COMMON+=($EXTRA_MPI_ARGS)

echo "============================================================"
echo " IMEX / cross-node NVLink bench   ${TS}"
echo " nodes=${NN} gpus/node=${GPUS_PER_NODE} ranks=${NP} tests=${ONLY}"
echo " node list: ${NODES[*]}"
echo " mpirun: $(command -v mpirun)"
echo " report: ${REPORT}"
echo "============================================================"

# ---------------------------------------------------------------- T0 check
if [ "$SKIP_CHECK" -eq 0 ]; then
    log "T0: imex-check.sh"
    if "${SCRIPT_DIR}/imex-check.sh" --quiet > "${RAW_DIR}/t0-imex-check.log" 2>&1; then
        result "T0 imex-check" PASS "$(grep '^PASS=' "${RAW_DIR}/t0-imex-check.log")"
    else
        cat "${RAW_DIR}/t0-imex-check.log"
        result "T0 imex-check" FAIL "fix domain first or use --skip-check"
        printf '%s\n' "${RESULTS[@]}"
        exit 1
    fi
fi

# ---------------------------------------------------------------- T1 fabric IPC
if want t1; then
    if [ -x "${TOOLS_DIR}/imex_fabric_ipc_test" ]; then
        log "T1: fabric handle import/export ${NODES[0]} <-> ${NODES[1]}"
        if mpirun "${MPI_COMMON[@]}" -np 2 -H "${NODES[0]}:1,${NODES[1]}:1" \
                "${TOOLS_DIR}/imex_fabric_ipc_test" --size 256 --iters 20 \
                > "${RAW_DIR}/t1-fabric-ipc-pair.log" 2>&1; then
            bw="$(grep -oE 'write: *[0-9.]+ GB/s' "${RAW_DIR}/t1-fabric-ipc-pair.log" | head -n 1)"
            result "T1 fabric IPC (2 nodes)" PASS "${bw}"
        else
            rc=$?
            tail -n 20 "${RAW_DIR}/t1-fabric-ipc-pair.log"
            [ "$rc" -eq 2 ] && result "T1 fabric IPC (2 nodes)" FAIL "FABRIC handles unsupported: IMEX down or GPU not in NVLink domain" \
                            || result "T1 fabric IPC (2 nodes)" FAIL "see ${RAW_DIR}/t1-fabric-ipc-pair.log"
        fi
        log "T1: all ${NN} nodes import from ${NODES[0]}"
        if mpirun "${MPI_COMMON[@]}" -np "$NN" -H "$(printf '%s:1,' "${NODES[@]}" | sed 's/,$//')" \
                "${TOOLS_DIR}/imex_fabric_ipc_test" --size 256 --iters 5 \
                > "${RAW_DIR}/t1-fabric-ipc-all.log" 2>&1; then
            result "T1 fabric IPC (all nodes)" PASS "$(grep -c 'PASS$' "${RAW_DIR}/t1-fabric-ipc-all.log") slices verified"
        else
            tail -n 20 "${RAW_DIR}/t1-fabric-ipc-all.log"
            result "T1 fabric IPC (all nodes)" FAIL "see ${RAW_DIR}/t1-fabric-ipc-all.log"
        fi
    else
        result "T1 fabric IPC" SKIP "${TOOLS_DIR}/imex_fabric_ipc_test missing (install_test_tools.sh)"
    fi
fi

# ---------------------------------------------------------------- T2 nvbandwidth
if want t2; then
    if [ -x "${TOOLS_DIR}/nvbandwidth" ] && "${TOOLS_DIR}/nvbandwidth" -l 2>/dev/null | grep -q 'multinode_device_to_device'; then
        log "T2: nvbandwidth multinode_* (2 nodes x ${GPUS_PER_NODE} GPUs)"
        # Built with -DMULTINODE=1 (install_test_tools.sh); one rank per GPU, all
        # ranks must be in one NVLink clique. Only rank 0 prints.
        if mpirun "${MPI_COMMON[@]}" -np $((2 * GPUS_PER_NODE)) -H "$HOSTS_PAIR" \
                "${TOOLS_DIR}/nvbandwidth" -t multinode_device_to_device_memcpy_read_ce multinode_device_to_device_memcpy_write_ce \
                > "${RAW_DIR}/t2-nvbandwidth.log" 2>&1; then
            summ="$(grep -E '^SUM ' "${RAW_DIR}/t2-nvbandwidth.log" | tr -s ' ' | head -n 4 | tr '\n' ';')"
            result "T2 nvbandwidth multinode" PASS "${summ:-see log}"
        else
            tail -n 20 "${RAW_DIR}/t2-nvbandwidth.log"
            result "T2 nvbandwidth multinode" FAIL "see ${RAW_DIR}/t2-nvbandwidth.log"
        fi
    else
        result "T2 nvbandwidth multinode" SKIP "nvbandwidth lacks multinode_* tests (build with -DMULTINODE=1 + MPI)"
    fi
fi

# ---------------------------------------------------------------- T3 NCCL
nccl_busbw() {
    # max "busbw" column (GB/s) of an nccl-tests output file
    awk '$1 ~ /^[0-9]+$/ && NF >= 12 { if ($(NF-1)+0 > m) m = $(NF-1)+0 } END { printf "%.1f", m }' "$1"
}
nccl_mnnvl_info() {
    local f="$1" cq
    cq="$(grep -oE 'MNNVL [01]( cliqueId [0-9x]+)?( cliqueSize [0-9]+)?' "$f" | sort -u | head -n 1)"
    [ -n "$cq" ] && echo "$cq" && return 0
    grep -qiE 'MNNVL.*(enabled|cliqueSize)' "$f" && echo "MNNVL on" && return 0
    echo "MNNVL info not in log"
}

if want t3; then
    NCCL_ENV=(-x NCCL_DEBUG=INFO -x "NCCL_DEBUG_SUBSYS=INIT,GRAPH,ENV")
    for t in sendrecv_perf alltoall_perf all_reduce_perf; do
        if [ ! -x "${TOOLS_DIR}/${t}" ]; then
            result "T3 ${t} MNNVL=1" SKIP "${TOOLS_DIR}/${t} missing"
            continue
        fi
        log "T3: ${t} NCCL_MNNVL_ENABLE=1 (${NP} ranks)"
        f="${RAW_DIR}/t3-${t}-mnnvl1.log"
        if mpirun "${MPI_COMMON[@]}" -np "$NP" -H "$HOSTS_ALL" "${NCCL_ENV[@]}" -x NCCL_MNNVL_ENABLE=1 \
                "${TOOLS_DIR}/${t}" -b 8 -e "$MAX_BYTES" -f 2 -g 1 > "$f" 2>&1; then
            info="$(nccl_mnnvl_info "$f")"
            transport="$(grep -oE 'via (NVLS|NVL|P2P/[A-Z]+|NET/[A-Za-z]+)' "$f" | sort | uniq -c | sort -rn | awk '{printf "%s:%s ", $3, $1}')"
            status=PASS
            echo "$info" | grep -q 'MNNVL 0' && status=WARN
            echo "$transport" | grep -q 'NET/' && status=WARN
            result "T3 ${t} MNNVL=1" "$status" "busbw max $(nccl_busbw "$f") GB/s; ${info}; ${transport}"
        else
            tail -n 15 "$f"
            result "T3 ${t} MNNVL=1" FAIL "see $f"
        fi
    done
    if [ -x "${TOOLS_DIR}/all_reduce_perf" ]; then
        log "T3: all_reduce_perf NCCL_MNNVL_ENABLE=0 (IB baseline)"
        f="${RAW_DIR}/t3-all_reduce_perf-mnnvl0.log"
        if mpirun "${MPI_COMMON[@]}" -np "$NP" -H "$HOSTS_ALL" "${NCCL_ENV[@]}" -x NCCL_MNNVL_ENABLE=0 \
                "${TOOLS_DIR}/all_reduce_perf" -b 8 -e "$MAX_BYTES" -f 2 -g 1 > "$f" 2>&1; then
            result "T3 all_reduce_perf MNNVL=0" PASS "busbw max $(nccl_busbw "$f") GB/s (baseline)"
            on="$(nccl_busbw "${RAW_DIR}/t3-all_reduce_perf-mnnvl1.log" 2>/dev/null || echo 0)"
            off="$(nccl_busbw "$f")"
            if awk -v a="$on" -v b="$off" 'BEGIN{exit !(a > 1.5*b)}'; then
                result "T3 MNNVL speedup" PASS "all_reduce ${on} vs ${off} GB/s ($(awk -v a="$on" -v b="$off" 'BEGIN{printf "%.1fx", (b>0)?a/b:0}'))"
            else
                result "T3 MNNVL speedup" WARN "all_reduce ${on} vs ${off} GB/s: MNNVL not faster than IB, traffic likely not on NVLink"
            fi
        else
            tail -n 15 "$f"
            result "T3 all_reduce_perf MNNVL=0" FAIL "see $f"
        fi
    fi
fi

echo
echo "================ IMEX bench summary (${TS}) ================"
printf '%s\n' "${RESULTS[@]}"
echo "raw logs: ${RAW_DIR}"
echo "report:   ${REPORT}"
if [ "$FAIL_N" -gt 0 ]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
