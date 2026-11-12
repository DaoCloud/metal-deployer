#!/bin/bash
# End-to-end test of S3 (testkit nic) and S4 (testkit net) without GPU hosts.
#
# Starts three hosts as systemd containers (netplan, systemd-networkd, sshd) on seven
# docker networks: LOM (ssh; a static netplan file with a default route, as delivered
# before testkit takes over), mgmt, storage and four "rails". A fake /sys inside each
# host maps the veth interfaces to BlueField-3 and ConnectX-8 PCI devices. testkit then
# discovers NICs, previews, applies and verifies netplan over real ssh (the LOM default
# route moves into a policy table), and a bad gateway checks that the automatic rollback
# restores the previous configuration.
#
# Usage: benchmarks/testkit/e2e/run-net-e2e.sh [--keep]
# Env:   UBUNTU_IMAGE (default ubuntu:24.04; e.g. docker.m.daocloud.io/library/ubuntu:24.04)
#        NET_PREFIX   (default 172.28; uses <prefix>.10/20/30/40-43.0/24)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TESTKIT=$(cd "$HERE/.." && pwd)
ROOT=${E2E_ROOT:-/tmp/testkit-net-e2e}
P=${NET_PREFIX:-172.28}
IMAGE=testkit-e2e-sdnode:latest
HOSTS=(gb300-r1-t01 gb300-r1-t02 gb300-r2-t01)
RACKS=(R1 R1 R2)
TRAYS=(1 2 1)
NETS=(lom:10 mgmt:20 stor:30 rail0:40 rail1:41 rail2:42 rail3:43)
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
PASSED=0
FAILED=0

log() { echo "[net-e2e] $*"; }
check() {
    local desc=$1
    shift
    if "$@"; then PASSED=$((PASSED + 1)); echo "  ok   $desc"; else FAILED=$((FAILED + 1)); echo "  FAIL $desc"; fi
}
cleanup() {
    if [ "$KEEP" = 1 ]; then log "kept hosts and $ROOT"; return; fi
    docker rm -f "${HOSTS[@]}" >/dev/null 2>&1 || true
    for n in "${NETS[@]}"; do docker network rm "testkit-net-${n%%:*}" >/dev/null 2>&1 || true; done
    rm -rf "$ROOT"
}
testkit() {
    local cfg=$1
    shift
    TESTKIT_NODE_SYSFS=/fake-sys PYTHONPATH="$TESTKIT" python3 -m testkit -c "$ROOT/$cfg" -t "$ROOT/tests.yaml" "$@"
}
on() { docker exec "$1" sh -c "$2"; }
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }
state() { json "$ROOT/runs/state.json" "d['hosts']['$1']['$2']['status']"; }

cluster() {   # cluster FILE [BAD_GW_HOST] [BAD_PCI_HOST]: base file + "testkit import" of a CSV IP plan
    cat > "$ROOT/$1" <<EOF
ssh: {user: root, key: $ROOT/id_ed25519, connect_timeout: 5, parallel: 8}
network:
  expect: {gpus: 0, cx8: {count: 4, link_layer: InfiniBand, rate_gbps: 800}, bf3: {pf_count: 2, mode: nic}, compare_golden: true}
  ib_mode: {enforce: true, reboot: never}
  lom: {table: 100, priority: 100}
  mgmt: {port: 0, default_route: true}
  storage: {port: 1}
  ipoib: {pkey: "0x7fff"}
  require_pci: true
  match_by: name
  rollback_seconds: 60
  verify: {ping_gateway: true, ping_peer_in_rack: true}
EOF
    local plan="$ROOT/${1%.yaml}.csv"
    echo "rack,tray,hostname,lom_ip,bf3_mgt_ip,bf3_mgt_gateway,bf3_mgt_pci,bf3_storage_ip,bf3_storage_gateway,bf3_storage_routes,bf3_storage_pci,ib_rail1_pci,ib_rail2_pci,ib_rail3_pci,ib_rail4_pci,ib_rail1_ip,ib_rail2_ip,ib_rail3_ip,ib_rail4_ip,ib_mtu" > "$plan"
    for i in 0 1 2; do
        local n=$((11 + i)) h=${HOSTS[$i]} gw=$P.20.1 mpci=0009:01:00.0
        [ "$h" = "${2:-}" ] && gw=$P.20.254
        [ "$h" = "${3:-}" ] && mpci=0009:01:00.7
        echo "${RACKS[$i]#R},${TRAYS[$i]},$h,$P.10.$n,$P.20.$n/24,$gw,$mpci,$P.30.$n/24,$P.30.1,10.98.0.0/16,0009:01:00.1,0000:03:00.0,0001:03:00.0,0002:03:00.0,0003:03:00.0,$P.40.$n/24,$P.41.$n/24,$P.42.$n/24,$P.43.$n/24,1300" >> "$plan"
    done
    testkit "$1" import "$plan" >/dev/null
}

setup() {
    log "building image"
    docker build -q --build-arg "BASE=${UBUNTU_IMAGE:-ubuntu:24.04}" -t "$IMAGE" -f "$HERE/sdnode.Dockerfile" "$HERE" >/dev/null
    cleanup
    mkdir -p "$ROOT"
    ssh-keygen -q -t ed25519 -N "" -f "$ROOT/id_ed25519"
    for n in "${NETS[@]}"; do docker network create --subnet "$P.${n#*:}.0/24" "testkit-net-${n%%:*}" >/dev/null; done
    for i in 0 1 2; do
        local h=${HOSTS[$i]} n=$((11 + i))
        docker run -d --name "$h" --hostname "$h" --privileged --cgroupns=host -e container=docker \
            -v /sys/fs/cgroup:/sys/fs/cgroup:rw --network testkit-net-lom --ip "$P.10.$n" \
            --mac-address "02:42:00:00:0a:1$((1 + i))" "$IMAGE" >/dev/null
        for net in "${NETS[@]:1}"; do
            docker network connect --ip "$P.${net#*:}.$n" "testkit-net-${net%%:*}" "$h"
        done
        docker cp "$ROOT/id_ed25519.pub" "$h:/root/.ssh/authorized_keys"
        docker cp "$HERE/fake_sysfs.py" "$h:/root/fake_sysfs.py"
        on "$h" "chown root:root /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
        # The delivered system: a static LOM with its default route, managed by networkd.
        on "$h" "printf 'network:\n  version: 2\n  ethernets:\n    lom:\n      match: {name: eth0}\n      addresses: [$P.10.$n/24]\n      routes:\n        - {to: default, via: $P.10.1}\n' > /etc/netplan/50-lom.yaml && chmod 600 /etc/netplan/50-lom.yaml"
    done
    sleep 3
    for h in "${HOSTS[@]}"; do
        # Docker's own default route was added before networkd ran; a booted host would not have it.
        on "$h" "ip route show default | while read -r r; do ip route del \$r; done; netplan apply 2>/dev/null; for _ in \$(seq 30); do ip route show default proto static | grep -q . && break; sleep 1; done"
    done
    for h in "${HOSTS[@]}"; do on "$h" "sed -i 's/172\.28\./$P./g' /root/fake_sysfs.py && python3 /root/fake_sysfs.py /fake-sys" >/dev/null; done
    printf 'images: {sb_worker: "x"}\nruntime: {results_dir: ./runs}\n' > "$ROOT/tests.yaml"
    cluster cluster.yaml
    cluster bad-gw.yaml gb300-r2-t01
    cluster bad-pci.yaml "" gb300-r1-t02
}

trap cleanup EXIT
setup

log "0. testkit import"
check "import kept the network section" grep -q "match_by: name" "$ROOT/cluster.yaml"
check "plan PCI imported" [ "$(grep -c 'mgmt_pci:' "$ROOT/cluster.yaml")" = 3 ]
out=$(testkit cluster.yaml validate 2>&1) || true
check "validate passes" grep -q "result: 0 error(s)" <<<"$out"

log "1. nic discover"
out=$(testkit cluster.yaml nic discover 2>&1) && rc=0 || rc=$?
check "discover exits 0" [ "$rc" = 0 ]
check "mgmt port found" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "d['mgmt_if']")" = eth1 ]
check "4 rails in PCI order" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "','.join(d['ib_devs'])")" = mlx5_0,mlx5_1,mlx5_2,mlx5_3 ]
check "LOM gateway found" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['lom']['gateway']")" = "$P.10.1" ]
check "golden match" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['checks']['golden_match']['result']")" = pass ]
check "plan PCI found on host" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['checks']['plan_pci']['result']")" = pass ]
out=$(testkit cluster.yaml nic show)
check "nic show prints rails" grep -q "mlx5_3" <<<"$out"

log "2. net apply --dry-run"
out=$(testkit cluster.yaml net apply --dry-run 2>&1) && rc=0 || rc=$?
check "dry-run exits 0" [ "$rc" = 0 ]
check "dry-run shows the netplan diff" grep -q "+    testkit-mgmt:" <<<"$out"
check "dry-run changes nothing" on gb300-r1-t01 "! ls /etc/netplan/90-static-* 2>/dev/null && ! grep -q table /etc/netplan/50-lom.yaml"
check "dry-run shows the LOM file change" grep -q "^+++ b/50-lom.yaml" <<<"$out"

log "3. net apply"
testkit cluster.yaml net apply >"$ROOT/apply.log" 2>&1 && rc=0 || rc=$?
cat "$ROOT/apply.log" | sed 's/^/     /' | tail -4
check "apply exits 0" [ "$rc" = 0 ]
check "every host passed" [ "$(state gb300-r1-t01 network)$(state gb300-r1-t02 network)$(state gb300-r2-t01 network)" = passpasspass ]
check "default route via mgmt only" [ "$(on gb300-r1-t02 "ip route show default")" = "default via $P.20.1 dev eth1 proto static " ]
check "mgmt+storage and compute files" [ "$(on gb300-r1-t01 "cd /etc/netplan && echo 90-*")" = \
    "90-static-compute.yaml 90-static-mgmt-storage.yaml" ]
check "LOM default route moved into table 100 in its own file" on gb300-r1-t02 "grep -q 'table: 100' /etc/netplan/50-lom.yaml"
check "LOM address kept" on gb300-r1-t02 "ip -4 addr show eth0 | grep -q 'inet $P.10.12/24'"
check "policy rule for the LOM address" on gb300-r1-t02 "ip rule show | grep -q 'from $P.10.12 lookup 100'"
check "LOM table has its default route" on gb300-r1-t02 "ip route show table 100 | grep -q 'default via $P.10.1 dev eth0'"
check "any network answered from the LOM address leaves via the LOM" \
    [ "$(on gb300-r1-t02 "ip -j route get 10.99.1.1 from $P.10.12" | json /dev/stdin "d[0]['dev']")" = eth0 ]
check "other traffic follows the mgmt default route" \
    [ "$(on gb300-r1-t02 "ip -j route get 10.99.1.1" | json /dev/stdin "d[0]['dev']")" = eth1 ]
check "BF3 MTU unchanged, IB MTU from plan" [ "$(on gb300-r2-t01 "cat /sys/class/net/eth1/mtu /sys/class/net/eth2/mtu /sys/class/net/eth6/mtu" | tr '\n' ' ')" = "1500 1500 1300 " ]
check "storage route from plan" on gb300-r2-t01 "ip route show 10.98.0.0/16 | grep -q 'via $P.30.1 dev eth2'"
check "rollback timer stopped" on gb300-r1-t01 "! systemctl is-active --quiet testkit-net-rollback.timer"
check "peer pings recorded" grep -q '"ping:ib3-peer"' "$ROOT/runs/$(ls "$ROOT/runs" | grep -E '^[0-9]{8}-' | sort | tail -1)/gb300-r1-t01/network/summary.json"

log "4. nic discover + net apply again (idempotent)"
testkit cluster.yaml nic discover >/dev/null 2>&1 || true
check "LOM gateway still known after S4 moved it to the LOM policy table" \
    [ "$(json "$ROOT/runs/nic/gb300-r1-t01.json" "d['lom']['gateway']")" = "$P.10.1" ]
out=$(testkit cluster.yaml net apply 2>&1) && rc=0 || rc=$?
check "second apply exits 0" [ "$rc" = 0 ]
check "reports no change" [ "$(grep -c 'no change' <<<"$out")" = 3 ]

log "5. bad gateway -> automatic rollback"
before=$(on gb300-r2-t01 "cat /etc/netplan/*.yaml | md5sum")
start=$(date +%s)
testkit bad-gw.yaml net apply --hosts gb300-r2-t01 >"$ROOT/rollback.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "apply exits 1" [ "$rc" = 1 ]
check "status is fail with rollback reason" grep -q "rolled back" "$ROOT/rollback.log"
check "old configuration restored" grep -q "old configuration restored" "$ROOT/rollback.log"
check "file content restored" [ "$(on gb300-r2-t01 "cat /etc/netplan/*.yaml | md5sum")" = "$before" ]
check "default route back to the good gateway" [ "$(on gb300-r2-t01 "ip route show default")" = "default via $P.20.1 dev eth1 proto static " ]
check "finished within rollback window (${took}s < 120s)" [ "$took" -lt 120 ]

log "6. net verify"
out=$(testkit cluster.yaml net verify 2>&1) && rc=0 || rc=$?
check "verify exits 0 after rollback" [ "$rc" = 0 ]

log "7. PCI in the plan not found on the host"
out=$(testkit bad-pci.yaml net apply --hosts gb300-r1-t02 2>&1) && rc=0 || rc=$?
check "apply refused" [ "$rc" = 1 ]
check "reason names the PCI" grep -q "mgmt_pci 0009:01:00.7 is not a BF3 port" <<<"$out"

log "8. conflicting netplan entry is refused"
on gb300-r1-t02 "printf 'network:\n  version: 2\n  ethernets:\n    other:\n      match: {name: eth1}\n      dhcp4: true\n' > /etc/netplan/55-other.yaml && chmod 600 /etc/netplan/55-other.yaml"
out=$(testkit cluster.yaml net apply --hosts gb300-r1-t02 2>&1) && rc=0 || rc=$?
check "apply refused" [ "$rc" = 1 ]
check "reason names the file" grep -q "55-other.yaml: ethernets.other" <<<"$out"
on gb300-r1-t02 "rm -f /etc/netplan/55-other.yaml"

log "9. report"
testkit cluster.yaml report >/dev/null
check "report has S3 and S4" python3 -c "
import json,re,sys; s=open('$ROOT/runs/report/report.html').read()
d=json.loads(re.search(r'const D = (.*);\n', s).group(1).replace('<\\\\/', '</'))
sys.exit(0 if {'nic','network'} <= {i['id'] for i in d['items']} else 1)"

echo
log "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
