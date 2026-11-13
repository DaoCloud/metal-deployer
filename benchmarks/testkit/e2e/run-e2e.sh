#!/bin/bash
# End-to-end test of the testkit executor without GPU hosts.
#
# Starts three fake hosts (containers with sshd and the docker CLI, using this machine's
# Docker daemon), then runs testkit against them over real ssh: per-node, mpi and delegate
# modes, a failing host, timeout, cancel, SIGTERM, status and logs.
#
# Usage: benchmarks/testkit/e2e/run-e2e.sh [--keep]
#   --keep   leave the fake hosts and files under $E2E_ROOT for inspection
# Needs: docker, python3 with PyYAML, ssh, ssh-keygen. Pulls docker:cli and debian:bookworm-slim.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TESTKIT=$(cd "$HERE/.." && pwd)
E2E_ROOT=${E2E_ROOT:-/tmp/testkit-e2e}
SSH_NET=${SSH_NET:-172.28.0}
MGMT_NET=${MGMT_NET:-172.29.0}
NODE_IMAGE=testkit-e2e-node:latest
WORKER_IMAGE=testkit-e2e-worker:latest
DCGM_IMAGE=${DCGM_IMAGE:-nvcr.io/nvidia/cloud-native/dcgm:4.7.1-ubuntu24.04}
SBCLI_IMAGE=testkit-e2e-sbcli:latest
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
    docker ps -aq --filter label=testkit.run | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker rm -f "${HOSTS[@]}" >/dev/null 2>&1 || true
    docker network rm testkit-e2e-ssh testkit-e2e-mgmt >/dev/null 2>&1 || true
    rm -rf "$E2E_ROOT"
}

testkit() {
    local dir=$1
    shift
    PYTHONPATH="$TESTKIT" python3 -m testkit -c "$E2E_ROOT/$dir/cluster.yaml" -t "$E2E_ROOT/$dir/tests.yaml" "$@"
}

# testkit_bg DIR ARGS...: start testkit in the background; $! is the python process itself.
testkit_bg() {
    local dir=$1
    shift
    (PYTHONPATH="$TESTKIT" exec python3 -m testkit -c "$E2E_ROOT/$dir/cluster.yaml" \
        -t "$E2E_ROOT/$dir/tests.yaml" "$@") &
}

# json FILE EXPR: print a value from a JSON file, EXPR is Python on variable d.
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }

state() { json "$E2E_ROOT/$1/runs/state.json" "d['hosts']['$2']['$3']['status']"; }

last_run() { ls "$E2E_ROOT/$1/runs" | grep -E '^[0-9]{8}-' | sort | tail -1; }

no_containers() { [ -z "$(docker ps -aq --filter "label=testkit.run=$1")" ]; }

no_locks() { ! ls "$E2E_ROOT/$1/runs/locks/"*.lock >/dev/null 2>&1; }

# scenario DIR SCOPE MODE SLEEP RUN_TIMEOUT [FAIL_HOST] [EXTRA_ITEMS]
scenario() {
    local dir=$1 scope=$2 mode=$3 sleep=$4 timeout=$5 fail_host=${6:-} extra=${7:-}
    mkdir -p "$E2E_ROOT/$dir"
    {
        echo "platform: gb300"
        echo "ssh: {user: root, key: $E2E_ROOT/id_ed25519, port: 22, parallel: 8, connect_timeout: 5}"
        echo "hosts:"
        local i=1
        for h in "${HOSTS[@]}"; do
            echo "  - {name: $h, rack: R1, tray: $i, ssh: $SSH_NET.1$i, mgmt: $MGMT_NET.1$i/24$( \
                [ "$h" = "$fail_host" ] && echo ", env: {TESTKIT_SMOKE_EXIT: '1'}")}"
            i=$((i + 1))
        done
    } > "$E2E_ROOT/$dir/cluster.yaml"
    cat > "$E2E_ROOT/$dir/tests.yaml" <<EOF
platform: gb300
images: {sb_worker: $WORKER_IMAGE, dcgm: $DCGM_IMAGE, sbcli: $SBCLI_IMAGE}
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
    docker build -q -t "$SBCLI_IMAGE" -f "$HERE/sbcli-stub.Dockerfile" "$HERE" >/dev/null
    cleanup
    mkdir -p "$E2E_ROOT/noderes"
    ssh-keygen -q -t ed25519 -N "" -f "$E2E_ROOT/id_ed25519"
    cp "$E2E_ROOT/id_ed25519.pub" "$E2E_ROOT/authorized_keys"
    chmod 600 "$E2E_ROOT/authorized_keys"
    docker network create --subnet "$SSH_NET.0/24" testkit-e2e-ssh >/dev/null
    docker network create --subnet "$MGMT_NET.0/24" testkit-e2e-mgmt >/dev/null
    local i=1
    for h in "${HOSTS[@]}"; do
        docker run -d --name "$h" --hostname "$h" --network testkit-e2e-ssh --ip "$SSH_NET.1$i" \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$E2E_ROOT/noderes:$E2E_ROOT/noderes" \
            "$NODE_IMAGE" >/dev/null
        docker cp "$E2E_ROOT/authorized_keys" "$h:/root/.ssh/authorized_keys"
        docker exec "$h" chown root:root /root/.ssh/authorized_keys
        docker network connect --ip "$MGMT_NET.1$i" testkit-e2e-mgmt "$h"
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
out=$(testkit s1 validate --ssh 2>&1) && rc=0 || rc=$?
check "validate exits 0" [ "$rc" = 0 ]
check "all hosts reachable" grep -q "ssh: 3/3 hosts reachable" <<<"$out"

log "2. per-node, scope node"
testkit s1 run smoke >"$E2E_ROOT/s1.log" 2>&1 && rc=0 || rc=$?
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
testkit s3 run smoke >"$E2E_ROOT/s3.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 1" [ "$rc" = 1 ]
check "failing host is fail" [ "$(state s3 gb300-r1-t02 smoke)" = fail ]
check "other hosts are stopped" [ "$(state s3 gb300-r1-t01 smoke)$(state s3 gb300-r1-t03 smoke)" = stoppedstopped ]
check "group stopped early (${took}s < 15s)" [ "$took" -lt 15 ]
check "containers removed" no_containers "$(last_run s3)"
out=$(testkit s3 run dcgm 2>&1) || true
check "failed host left out of later items" grep -q "dcgm: gb300-r1-t02 left out (smoke fail" <<<"$out"

log "4. run_timeout"
scenario s4 node per-node 120 5
start=$(date +%s)
testkit s4 run smoke >"$E2E_ROOT/s4.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "run exits 1" [ "$rc" = 1 ]
check "status is timeout" [ "$(state s4 gb300-r1-t01 smoke)" = timeout ]
check "stopped soon after the timeout (${took}s < 20s)" [ "$took" -lt 20 ]
check "containers removed" no_containers "$(last_run s4)"

log "5. mpi mode"
scenario s5 rack mpi 0 120
testkit s5 run smoke >"$E2E_ROOT/s5.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s5)
check "run exits 0" [ "$rc" = 0 ]
check "mpirun reached all 3 hosts" [ "$(grep -c '^gb300-r1-t0' "$E2E_ROOT/s5/runs/$run/gb300-r1-t01/smoke/launcher.log" || true)" = 3 ]
check "rank 1 recorded as group result" [ "$(json "$E2E_ROOT/s5/runs/state.json" "d['hosts']['gb300-r1-t02']['smoke']['group_result']")" = True ]
check "containers removed" no_containers "$run"

log "6. delegate mode"
scenario s6 group delegate 0 60
testkit s6 run smoke >"$E2E_ROOT/s6.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s6)
check "run exits 0" [ "$rc" = 0 ]
check "only the master ran a container" [ -f "$E2E_ROOT/s6/runs/$run/gb300-r1-t01/smoke/container.log" ] && \
    [ ! -f "$E2E_ROOT/s6/runs/$run/gb300-r1-t02/smoke/container.log" ]

log "7. testkit cancel"
scenario s7 rack per-node 120 300
testkit_bg s7 run smoke >"$E2E_ROOT/s7.log" 2>&1
pid=$!
for _ in $(seq 60); do
    run=$(last_run s7 2>/dev/null || true)
    [ -n "$run" ] && [ "$(docker ps -q --filter "label=testkit.run=$run" | wc -l)" = 3 ] && break
    sleep 1
done
testkit s7 cancel "$run" >/dev/null
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
testkit_bg s8 run smoke >"$E2E_ROOT/s8.log" 2>&1
pid=$!
for _ in $(seq 60); do
    run=$(last_run s8 2>/dev/null || true)
    [ -n "$run" ] && [ "$(docker ps -q --filter "label=testkit.run=$run" | wc -l)" = 3 ] && break
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
out=$(testkit s3 status)
check "status counts the failed host" grep -q "gb300-r1-t02 .*smoke .*fail" <<<"$out"
out=$(testkit s1 logs "$(last_run s1)" --host gb300-r1-t02)
check "logs prints the container log" grep -q "smoke: host gb300-r1-t02" <<<"$out"

log "10. dcgm (official DCGM image, no GPUs here)"
if docker image inspect "$DCGM_IMAGE" >/dev/null 2>&1 || docker pull -q "$DCGM_IMAGE" >/dev/null 2>&1; then
    scenario s10 node per-node 0 300 "" "dcgm: {enabled: true, scope: node, hosts: all, hostengine: container, level: 1}"
    testkit s10 run dcgm >"$E2E_ROOT/s10.log" 2>&1 && rc=0 || rc=$?
    run=$(last_run s10)
    d="$E2E_ROOT/s10/runs/$run/gb300-r1-t01/dcgm"
    check "run exits 1 (no GPUs)" [ "$rc" = 1 ]
    check "raw diag output copied back" grep -q '"DCGM Diagnostic"' "$d/raw/diag.json"
    check "diag error parsed into the reason" grep -q "No available testing entities" <<<"$(state s10 gb300-r1-t01 dcgm; json "$E2E_ROOT/s10/runs/state.json" "d['hosts']['gb300-r1-t01']['dcgm']['reason']")"
    check "env.json reports DCGM 4.7.1" [ "$(json "$d/env.json" "d['dcgm']")" = 4.7.1 ]
    check "summary has checks" [ "$(json "$d/summary.json" "d['checks']['gpu_count']['result']")" = fail ]
    check "containers removed" no_containers "$run"
    sed -i 's/hostengine: container/hostengine: host/' "$E2E_ROOT/s10/tests.yaml"
    testkit s10 run dcgm >"$E2E_ROOT/s10b.log" 2>&1 || true
    check "host mode without host hostengine is an error" [ "$(state s10 gb300-r1-t02 dcgm)" = error ]
    check "error explains the missing hostengine" grep -q "cannot reach the host nv-hostengine" \
        "$E2E_ROOT/s10/runs/$(last_run s10)/gb300-r1-t02/dcgm/container.log"
else
    log "   skipped: cannot pull $DCGM_IMAGE"
fi

log "11. superbench (stub sb: real ssh with the run key, SuperBench result format)"
scenario s11 node per-node 0 300 "" "superbench:
  common: {rule: rule_gb300.yaml, timeout: 120}
  single_node: {enabled: true, scope: node, hosts: all, config: config_gb300_single_node.yaml, benchmarks: {exclude: [gpu-burn]}}
  rack: {enabled: true, scope: rack, hosts: all, config: config_gb300_multi_node.yaml}"
sed -i 's/^smoke: {enabled: true/smoke: {enabled: false/' "$E2E_ROOT/s11/tests.yaml"
testkit s11 run superbench >"$E2E_ROOT/s11.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s11)
check "run exits 0" [ "$rc" = 0 ]
check "single-node: every host passed" [ "$(state s11 gb300-r1-t01 superbench.single_node)$(state s11 gb300-r1-t02 superbench.single_node)$(state s11 gb300-r1-t03 superbench.single_node)" = passpasspass ]
check "single-node ran once for all hosts" [ "$(json "$E2E_ROOT/s11/runs/$run/run.json" "list(d['items']['superbench.single_node']['groups'])")" = "['all']" ]
check "exclude reached sb" grep -q 'superbench.enable=\["cublaslt-gemm:compute","nccl-bw:bw:allreduce:nvlink"\]' \
    "$E2E_ROOT/s11/runs/$run/gb300-r1-t01/superbench.single_node/sb/run/override.txt"
check "per-host metrics split out" [ "$(json "$E2E_ROOT/s11/runs/$run/gb300-r1-t03/superbench.single_node/summary.json" "d['metrics']['cublaslt-gemm:compute/fp8e4m3_0_8192_8192_8192_flops']['value']")" = 4477.0 ]
check "rack: fixed pairs t01-t02, then t03-t01 (odd host)" [ "$(json "$E2E_ROOT/s11/runs/$run/_groups/superbench.rack/R1/summary.json" "sorted(d['rdma']['pairs'])")" = "['gb300-r1-t01|gb300-r1-t02', 'gb300-r1-t01|gb300-r1-t03']" ]
check "rack: pairing file by SuperBench hostfile index" [ "$(cat "$E2E_ROOT/s11/runs/$run/gb300-r1-t01/superbench.rack/raw/ib-pairs.txt" | tr '\n' ' ')" = "0,1 2,0 " ]
check "rack: pairing files removed from the hosts" [ "$(for h in gb300-r1-t01 gb300-r1-t02 gb300-r1-t03; do docker exec $h sh -c "ls /mnt/testkit/$run 2>/dev/null | wc -l"; done | tr -d '\n')" = 000 ]
check "run key removed from every host" [ "$(for h in gb300-r1-t01 gb300-r1-t02 gb300-r1-t03; do docker exec $h grep -c testkit- /root/.ssh/authorized_keys; done | tr -d '\n')" = 000 ]
check "private run key removed from the rank 0 host" [ -z "$(find "$E2E_ROOT/noderes/$run" -name id_ed25519)" ]
testkit s11 report >/dev/null
check "report has the RDMA matrix" python3 -c "
import json,re,sys; s=open('$E2E_ROOT/s11/runs/report/report.html').read()
d=json.loads(re.search(r'const D = (.*);\n', s).group(1).replace('<\\\\/', '</'))
sys.exit(0 if d['rdma'] and len(d['rdma']['pairs']) == 2 and d['rdma']['rounds'] == 2 else 1)"

log "12. imex (stub IMEX tools on the hosts) + nvl-domain + SHARP (stub sb)"
for h in gb300-r1-t01 gb300-r1-t02 gb300-r1-t03; do
    docker exec "$h" sh -c 'mkdir -p /opt/resource/scripts
cat > /opt/resource/scripts/imex-configure.sh <<"EOS"
#!/bin/sh
cp "$2" /tmp/imex-nodes.list && echo "nodes_config.cfg written: $(grep -c . "$2") nodes"
EOS
cat > /opt/resource/scripts/imex-check.sh <<"EOS"
#!/bin/sh
echo "================ IMEX check summary ================"
echo "L1  PASS  local daemon state                  READY"
EOS
cat > /usr/local/bin/nvidia-imex-ctl <<"EOS"
#!/bin/sh
i=0; for ip in $(cat /tmp/imex-nodes.list); do echo "Node #$i - $ip - READY - Version: 580"; i=$((i+1)); done
echo " Nodes From\\To  0   1   2"; for r in 0 1 2; do echo "       $r        C   C   C"; done
echo "Domain State: UP"
EOS
chmod +x /opt/resource/scripts/*.sh /usr/local/bin/nvidia-imex-ctl'
done
scenario s12 rack mpi 0 300 "" "imex: {enabled: true, scope: rack, hosts: all, wait_ready_seconds: 30}
superbench:
  nvl_domain: {enabled: true, scope: rack, hosts: all, config: config_gb300_nvl_domain.yaml}
  all: {enabled: true, scope: group, hosts: all, config: config_gb300_all_nodes.yaml}
sharp:
  common: {ops: [allreduce, allgather]}
  scale_out: {enabled: true, scope: rack, hosts: all, cases: [O-A, O-B]}"
sed -i 's/^smoke: {enabled: true/smoke: {enabled: false/' "$E2E_ROOT/s12/tests.yaml"
testkit s12 run imex superbench sharp >"$E2E_ROOT/s12-gate.log" 2>&1 || true
check "imex is a gate: the run stops before nvl-domain" grep -q "gate: imex fail" "$E2E_ROOT/s12-gate.log"
check "gate stop prints how to resume" grep -q "testkit run --from imex" "$E2E_ROOT/s12-gate.log"
testkit s12 run imex superbench sharp --no-gate >"$E2E_ROOT/s12.log" 2>&1 && rc=0 || rc=$?
run=$(last_run s12)
g="$E2E_ROOT/s12/runs/$run/_groups"
check "IMEX configured on every host with the same list" [ "$(for h in gb300-r1-t01 gb300-r1-t02 gb300-r1-t03; do docker exec $h md5sum /tmp/imex-nodes.list; done | awk '{print $1}' | sort -u | wc -l)" = 1 ]
check "domain matrix parsed" [ "$(json "$g/imex/R1/summary.json" "len(d['matrix']['cells'])")" = 3 ]
check "T1 ran over mpirun and its build failure is reported" grep -q "T1 编译 imex_fabric_ipc_test" "$g/imex/R1/summary.json"
check "T2 off by default (nvlink.pairs covers the rack)" python3 -c "
import json,sys; rows=json.load(open('$g/imex/R1/summary.json'))['tables'][0]['rows']
sys.exit(0 if not any(r[0]=='T2 nvbandwidth multinode' and r[1]=='pass' for r in rows) else 1)"
check "imex group failed only on T1, hosts not blamed" [ "$(state s12 gb300-r1-t02 imex)" = fail ] && \
    [ "$(json "$E2E_ROOT/s12/runs/state.json" "d['hosts']['gb300-r1-t02']['imex']['group_result']")" = True ]
check "nvl-domain ran after imex (group result, not excluded)" [ "$(state s12 gb300-r1-t01 superbench.nvl_domain)" = pass ]
check "IB SHARP O-B/O-A compared" python3 -c "
import json,sys; t=json.load(open('$g/sharp.scale_out/R1/summary.json'))['tables']
sys.exit(0 if ['allreduce', 'O-B / O-A', 1.5] in t[1]['rows'] and ['allgather', 'O-B / O-A', 1.5] in t[1]['rows'] else 1)"
check "SHARP NCCL log evidence collected" grep -q "uses Algo NVLS" "$g/sharp.scale_out/R1/summary.json"
check "NCCL all racks: small-message time in the table" python3 -c "
import json,sys,glob; t=json.load(open(glob.glob('$g/superbench.all/*/summary.json')[0]))['tables'][0]
sys.exit(0 if t['rows'][0][3] == 25.0 and t['head'][3] == '8 B 时延 us' else 1)"
check "NCCL log: IB rails only (hca check)" python3 -c "
import json,sys,glob; t=json.load(open(glob.glob('$g/superbench.all/*/summary.json')[0]))['tables']
sys.exit(0 if any(x['title'].startswith('NCCL 网卡使用') for x in t) else 1)"

log "13. report"
out=$(testkit s3 report --archive) && rc=0 || rc=$?
check "report exits 0" [ "$rc" = 0 ]
check "report.html written" grep -q "const D = " "$E2E_ROOT/s3/runs/report/report.html"
check "hosts.csv marks the failing host" grep -q '^gb300-r1-t02,R1,2,,fail,' "$E2E_ROOT/s3/runs/report/hosts.csv"
check "raw logs archived" tar -tzf "$E2E_ROOT/s3/runs/report/raw-logs.tar.gz" >/dev/null

echo
log "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
