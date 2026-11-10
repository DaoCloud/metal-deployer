#!/bin/bash
# End-to-end test of the mtest executor without GPU hosts.
#
# Starts three fake hosts (containers with sshd and the docker CLI, using this machine's
# Docker daemon), then runs mtest against them over real ssh: per-node, mpi and delegate
# modes, a failing host, timeout, cancel, SIGTERM, status and logs.
#
# Usage: benchmarks/testkit/e2e/run-e2e.sh [--keep]
#   --keep   leave the fake hosts and files under $E2E_ROOT for inspection
# Needs: docker, python3 with PyYAML, ssh, ssh-keygen. Pulls docker:cli and debian:bookworm-slim.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TESTKIT=$(cd "$HERE/.." && pwd)
E2E_ROOT=${E2E_ROOT:-/tmp/mtest-e2e}
SSH_NET=${SSH_NET:-172.28.0}
MGMT_NET=${MGMT_NET:-172.29.0}
NODE_IMAGE=mtest-e2e-node:latest
WORKER_IMAGE=mtest-e2e-worker:latest
HOSTS=(gb300-r1-t01 gb300-r1-t02 gb300-r1-t03)
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

PASSED=0
FAILED=0

log() { echo "[e2e] $*"; }

check() {
    local desc=$1
    shift
    if "$@"; then
        PASSED=$((PASSED + 1))
        echo "  ok   $desc"
    else
        FAILED=$((FAILED + 1))
        echo "  FAIL $desc"
    fi
}

cleanup() {
    if [ "$KEEP" = 1 ]; then
        log "kept fake hosts and $E2E_ROOT"
        return
    fi
    docker ps -aq --filter label=mtest.run | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker rm -f "${HOSTS[@]}" >/dev/null 2>&1 || true
    docker network rm mtest-e2e-ssh mtest-e2e-mgmt >/dev/null 2>&1 || true
    rm -rf "$E2E_ROOT"
}

mtest() {
    local dir=$1
    shift
    PYTHONPATH="$TESTKIT" python3 -m mtest -c "$E2E_ROOT/$dir/cluster.yaml" -t "$E2E_ROOT/$dir/tests.yaml" "$@"
}

# mtest_bg DIR ARGS...: start mtest in the background; $! is the python process itself.
mtest_bg() {
    local dir=$1
    shift
    (PYTHONPATH="$TESTKIT" exec python3 -m mtest -c "$E2E_ROOT/$dir/cluster.yaml" \
        -t "$E2E_ROOT/$dir/tests.yaml" "$@") &
}

# json FILE EXPR: print a value from a JSON file, EXPR is Python on variable d.
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }

state() { json "$E2E_ROOT/$1/runs/state.json" "d['hosts']['$2']['$3']['status']"; }

last_run() { ls "$E2E_ROOT/$1/runs" | grep -E '^[0-9]{8}-' | sort | tail -1; }

no_containers() { [ -z "$(docker ps -aq --filter "label=mtest.run=$1")" ]; }

no_locks() { ! ls "$E2E_ROOT/$1/runs/locks/"*.lock >/dev/null 2>&1; }

# scenario DIR SCOPE MODE SLEEP RUN_TIMEOUT [FAIL_HOST] [EXTRA_ITEMS]
scenario() {
    local dir=$1 scope=$2 mode=$3 sleep=$4 timeout=$5 fail_host=${6:-} extra=${7:-}
    mkdir -p "$E2E_ROOT/$dir"
    {
        echo "ssh: {user: root, key: $E2E_ROOT/id_ed25519, port: 22, parallel: 8, connect_timeout: 5}"
        echo "hosts:"
        local i=1
        for h in "${HOSTS[@]}"; do
            echo "  - {name: $h, rack: R1, tray: $i, ssh: $SSH_NET.1$i, mgmt: $MGMT_NET.1$i/24$( \
                [ "$h" = "$fail_host" ] && echo ", env: {MTEST_SMOKE_EXIT: '1'}")}"
            i=$((i + 1))
        done
    } > "$E2E_ROOT/$dir/cluster.yaml"
    cat > "$E2E_ROOT/$dir/tests.yaml" <<EOF
images: {sb_worker: $WORKER_IMAGE, dcgm: $WORKER_IMAGE}
image_pull: skip
runtime:
  results_dir: ./runs
  node_results_dir: $E2E_ROOT/noderes
  container_args: [--network, "container:{hostname}"]
  run_timeout: $timeout
  rendezvous_timeout: 60
  poll_interval: 1
smoke: {enabled: true, scope: $scope, hosts: all, mode: $mode, sleep: $sleep}
$extra
EOF
}

setup() {
    log "building images"
    docker build -q -t "$NODE_IMAGE" -f "$HERE/node.Dockerfile" "$HERE" >/dev/null
    docker build -q -t "$WORKER_IMAGE" -f "$HERE/worker.Dockerfile" "$HERE" >/dev/null
    cleanup
    mkdir -p "$E2E_ROOT/noderes"
    ssh-keygen -q -t ed25519 -N "" -f "$E2E_ROOT/id_ed25519"
    cp "$E2E_ROOT/id_ed25519.pub" "$E2E_ROOT/authorized_keys"
    chmod 600 "$E2E_ROOT/authorized_keys"
    docker network create --subnet "$SSH_NET.0/24" mtest-e2e-ssh >/dev/null
    docker network create --subnet "$MGMT_NET.0/24" mtest-e2e-mgmt >/dev/null
    local i=1
    for h in "${HOSTS[@]}"; do
        docker run -d --name "$h" --hostname "$h" --network mtest-e2e-ssh --ip "$SSH_NET.1$i" \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$E2E_ROOT/noderes:$E2E_ROOT/noderes" \
            -v "$E2E_ROOT/authorized_keys:/root/.ssh/authorized_keys:ro" \
            "$NODE_IMAGE" >/dev/null
        docker network connect --ip "$MGMT_NET.1$i" mtest-e2e-mgmt "$h"
        i=$((i + 1))
    done
    for i in 1 2 3; do
        for _ in $(seq 30); do
            ssh -q -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                -i "$E2E_ROOT/id_ed25519" "root@$SSH_NET.1$i" true 2>/dev/null && break
            sleep 1
        done
    done
}

trap cleanup EXIT
setup

log "1. validate --ssh"
scenario s1 node per-node 0 60
out=$(mtest s1 validate --ssh 2>&1) && rc=0 || rc=$?
check "validate exits 0" [ "$rc" = 0 ]
check "all hosts reachable" grep -q "ssh: 3/3 hosts reachable" <<<"$out"

log "2. per-node, scope node"
mtest s1 run smoke >"$E2E_ROOT/s1.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s1)
check "run exits 0" [ "$rc" = 0 ]
check "every host passed" [ "$(state s1 gb300-r1-t01 smoke)$(state s1 gb300-r1-t02 smoke)$(state s1 gb300-r1-t03 smoke)" = passpasspass ]
check "summary copied back" [ "$(json "$E2E_ROOT/s1/runs/$run/gb300-r1-t02/smoke/summary.json" "d['status'] + str(d['rank'])")" = pass0 ]
check "container log copied back" grep -q "smoke: host gb300-r1-t03" "$E2E_ROOT/s1/runs/$run/gb300-r1-t03/smoke/container.log"
check "containers removed" no_containers "$run"
check "locks released" no_locks s1

log "3. per-node, scope rack, one host fails"
scenario s3 rack per-node 30 120 gb300-r1-t02 "dcgm: {enabled: true, scope: node, hosts: all}"
start=$(date +%s)
mtest s3 run smoke >"$E2E_ROOT/s3.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 1" [ "$rc" = 1 ]
check "failing host is fail" [ "$(state s3 gb300-r1-t02 smoke)" = fail ]
check "other hosts are stopped" [ "$(state s3 gb300-r1-t01 smoke)$(state s3 gb300-r1-t03 smoke)" = stoppedstopped ]
check "group stopped early (${took}s < 15s)" [ "$took" -lt 15 ]
check "containers removed" no_containers "$(last_run s3)"
out=$(mtest s3 run dcgm 2>&1) || true
check "failed host left out of later items" grep -q "dcgm: gb300-r1-t02 left out (smoke fail" <<<"$out"

log "4. run_timeout"
scenario s4 node per-node 120 5
start=$(date +%s)
mtest s4 run smoke >"$E2E_ROOT/s4.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 1" [ "$rc" = 1 ]
check "status is timeout" [ "$(state s4 gb300-r1-t01 smoke)" = timeout ]
check "stopped soon after the timeout (${took}s < 20s)" [ "$took" -lt 20 ]
check "containers removed" no_containers "$(last_run s4)"

log "5. mpi mode"
scenario s5 rack mpi 0 120
mtest s5 run smoke >"$E2E_ROOT/s5.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s5)
check "run exits 0" [ "$rc" = 0 ]
check "mpirun reached all 3 hosts" [ "$(grep -c '^gb300-r1-t0' "$E2E_ROOT/s5/runs/$run/gb300-r1-t01/smoke/launcher.log" || true)" = 3 ]
check "rank 1 recorded as group result" [ "$(json "$E2E_ROOT/s5/runs/state.json" "d['hosts']['gb300-r1-t02']['smoke']['group_result']")" = True ]
check "containers removed" no_containers "$run"

log "6. delegate mode"
scenario s6 group delegate 0 60
mtest s6 run smoke >"$E2E_ROOT/s6.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s6)
check "run exits 0" [ "$rc" = 0 ]
check "only the master ran a container" [ -f "$E2E_ROOT/s6/runs/$run/gb300-r1-t01/smoke/container.log" ] && \
    [ ! -f "$E2E_ROOT/s6/runs/$run/gb300-r1-t02/smoke/container.log" ]

log "7. mtest cancel"
scenario s7 rack per-node 120 300
mtest_bg s7 run smoke >"$E2E_ROOT/s7.log" 2>&1
pid=$!
for _ in $(seq 60); do
    run=$(last_run s7 2>/dev/null || true)
    [ -n "$run" ] && [ "$(docker ps -q --filter "label=mtest.run=$run" | wc -l)" = 3 ] && break
    sleep 1
done
mtest s7 cancel "$run" >/dev/null
start=$(date +%s)
wait "$pid" && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 130" [ "$rc" = 130 ]
check "stopped within ${took}s (< 15s)" [ "$took" -lt 15 ]
check "status is cancelled" [ "$(state s7 gb300-r1-t01 smoke)" = cancelled ]
check "containers removed" no_containers "$run"
check "locks released" no_locks s7

log "8. SIGTERM"
scenario s8 rack per-node 120 300
mtest_bg s8 run smoke >"$E2E_ROOT/s8.log" 2>&1
pid=$!
for _ in $(seq 60); do
    run=$(last_run s8 2>/dev/null || true)
    [ -n "$run" ] && [ "$(docker ps -q --filter "label=mtest.run=$run" | wc -l)" = 3 ] && break
    sleep 1
done
kill -TERM "$pid"
start=$(date +%s)
wait "$pid" && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 130" [ "$rc" = 130 ]
check "stopped within ${took}s (< 15s)" [ "$took" -lt 15 ]
check "containers removed" no_containers "$run"
check "run.json says cancelled" [ "$(json "$E2E_ROOT/s8/runs/$run/run.json" "d['status']")" = cancelled ]

log "9. status and logs"
out=$(mtest s3 status)
check "status counts the failed host" grep -q "gb300-r1-t02 .*smoke .*fail" <<<"$out"
out=$(mtest s1 logs "$(last_run s1)" --host gb300-r1-t02)
check "logs prints the container log" grep -q "smoke: host gb300-r1-t02" <<<"$out"

echo
log "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
