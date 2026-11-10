#!/bin/sh
# S5 DCGM diagnostics, run inside nvcr.io/nvidia/cloud-native/dcgm (no python in that image).
# Only collects raw output into /results/raw; mtest parses it on the control node.
#
# Env: MTEST_DCGM_MODE   host: use the host's nv-hostengine (127.0.0.1:5555, host network)
#                        container: start nv-hostengine in this container (host service stopped by mtest)
#      MTEST_DCGM_LEVEL  dcgmi diag -r level (1-4)
#      MTEST_DCGM_HEALTH 1: also run dcgmi health
# Exit: 0 when the diagnostic ran (pass or fail is decided from the output), 2 on execution errors.
set -u
R=/results/raw
mkdir -p "$R"
MODE=${MTEST_DCGM_MODE:-host}
LEVEL=${MTEST_DCGM_LEVEL:-3}
LOG=mtest-${MTEST_RUN_ID:-run}.log
date -u +%Y-%m-%dT%H:%M:%SZ > "$R/started_at"

if [ "$MODE" = container ]; then
    nv-hostengine -n --log-filename /var/log/nvidia-dcgm/nv-hostengine.log > "$R/nv-hostengine.out" 2>&1 &
    i=0
    until dcgmi discovery -l > /dev/null 2>&1; do
        i=$((i + 1))
        [ $i -gt 30 ] && { echo "nv-hostengine did not start"; cat "$R/nv-hostengine.out"; exit 2; }
        sleep 1
    done
elif ! dcgmi discovery -l > /dev/null 2>&1; then
    echo "cannot reach the host nv-hostengine on 127.0.0.1:5555 (is nvidia-dcgm running on the host?)"
    exit 2
fi

dcgmi -v > "$R/dcgmi-version.txt" 2>&1
dcgmi discovery -l > "$R/discovery.txt" 2>&1
nvidia-smi -q > "$R/nvidia-smi-q.txt" 2>&1
nvidia-smi nvlink -s > "$R/nvlink.txt" 2>&1
nvidia-smi --query-gpu=index,name,pci.bus_id,serial,vbios_version,driver_version --format=csv,noheader \
    > "$R/gpus.csv" 2>&1

if [ "${MTEST_DCGM_HEALTH:-1}" = 1 ]; then
    dcgmi health -s a > /dev/null 2>&1
    dcgmi health -c -j > "$R/health.json" 2>&1
fi

echo "dcgmi diag -r $LEVEL (this takes a while)"
dcgmi diag -r "$LEVEL" -j --log-filename "$LOG" > "$R/diag.json" 2> "$R/diag.stderr"
echo $? > "$R/diag.rc"

# The diag log is written next to the hostengine log (host mode: host directory mounted read-only).
for d in /var/log/nvidia-dcgm /host-dcgm-log; do
    [ -f "$d/nvvs/$LOG" ] && cp "$d/nvvs/$LOG" "$R/diag.log"
    [ -f "$d/$LOG" ] && cp "$d/$LOG" "$R/diag.log"
done

drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
cuda=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)
vbios=$(nvidia-smi --query-gpu=vbios_version --format=csv,noheader 2>/dev/null | sort -u | tr '\n' ' ' | sed 's/ $//')
dcgm=$(sed -n 's/^Version *: *//p' "$R/dcgmi-version.txt" | head -1)
printf '{"driver": "%s", "cuda": "%s", "vbios": "%s", "dcgm": "%s", "dcgm_mode": "%s"}\n' \
    "$drv" "$cuda" "$vbios" "$dcgm" "$MODE" > /results/env.json
date -u +%Y-%m-%dT%H:%M:%SZ > "$R/finished_at"
[ -s "$R/diag.json" ] || { echo "dcgmi diag produced no output"; cat "$R/diag.stderr"; exit 2; }
echo "done: diag exit $(cat "$R/diag.rc")"
exit 0
