#!/bin/bash
# End-to-end test of S3 (mtest nic) and S4 (mtest net) without GPU hosts.
#
# Starts three hosts as systemd containers (netplan, systemd-networkd, sshd) on seven
# docker networks: LOM (ssh, addresses from a dnsmasq DHCP server like on real hosts),
# mgmt, storage and four "rails". A fake /sys inside each
# host maps the veth interfaces to BlueField-3 and ConnectX-8 PCI devices. mtest then
# discovers NICs, previews, applies and verifies netplan over real ssh, and a bad
# gateway checks that the automatic rollback restores the previous configuration.
#
# Usage: benchmarks/testkit/e2e/run-net-e2e.sh [--keep]
# Env:   UBUNTU_IMAGE (default ubuntu:24.04; e.g. docker.m.daocloud.io/library/ubuntu:24.04)
#        NET_PREFIX   (default 172.28; uses <prefix>.10/20/30/40-43.0/24)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TESTKIT=$(cd "$HERE/.." && pwd)
ROOT=${E2E_ROOT:-/tmp/mtest-net-e2e}
P=${NET_PREFIX:-172.28}
IMAGE=mtest-e2e-sdnode:latest
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
    docker rm -f "${HOSTS[@]}" mtest-net-dhcp >/dev/null 2>&1 || true
    for n in "${NETS[@]}"; do docker network rm "mtest-net-${n%%:*}" >/dev/null 2>&1 || true; done
    rm -rf "$ROOT"
}
mtest() {
    local cfg=$1
    shift
    MTEST_NODE_SYSFS=/fake-sys PYTHONPATH="$TESTKIT" python3 -m mtest -c "$ROOT/$cfg" -t "$ROOT/tests.yaml" "$@"
}
on() { docker exec "$1" sh -c "$2"; }
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }
state() { json "$ROOT/runs/state.json" "d['hosts']['$1']['$2']['status']"; }

cluster() {   # cluster FILE GATEWAY
    {
        echo "ssh: {user: root, key: $ROOT/id_ed25519, connect_timeout: 5, parallel: 8}"
        echo "network:"
        echo "  expect: {cx8: {count: 4, link_layer: InfiniBand, rate_gbps: 800}, bf3: {pf_count: 2, mode: nic}, compare_golden: true}"
        echo "  ib_mode: {enforce: true, reboot: never}"
        echo "  lom: {pin_routes: [10.99.0.0/16]}"
        echo "  mgmt: {port: 0, gateway: $2, mtu: 1400, default_route: true}"
        echo "  storage: {port: 1, mtu: 1450, routes: [10.98.0.0/16]}"
        echo "  ipoib: {mtu: 1300}"
        echo "  match_by: name"
        echo "  rollback_seconds: 60"
        echo "  verify: {ping_gateway: true, ping_peer_in_rack: true}"
        echo "hosts:"
        for i in 0 1 2; do
            local n=$((11 + i))
            echo "  - {name: ${HOSTS[$i]}, rack: ${RACKS[$i]}, tray: ${TRAYS[$i]}, ssh: $P.10.$n, mgmt: $P.20.$n/24,"
            echo "     storage: $P.30.$n/24, ib: [$P.40.$n/24, $P.41.$n/24, $P.42.$n/24, $P.43.$n/24]$( \
                [ $i = 0 ] && echo ", tags: [golden]")}"
        done
    } > "$ROOT/$1"
}

setup() {
    log "building image"
    docker build -q --build-arg "BASE=${UBUNTU_IMAGE:-ubuntu:24.04}" -t "$IMAGE" -f "$HERE/sdnode.Dockerfile" "$HERE" >/dev/null
    docker build -q -t mtest-e2e-dhcp -f "$HERE/dhcp.Dockerfile" "$HERE" >/dev/null
    cleanup
    mkdir -p "$ROOT"
    ssh-keygen -q -t ed25519 -N "" -f "$ROOT/id_ed25519"
    for n in "${NETS[@]}"; do docker network create --subnet "$P.${n#*:}.0/24" "mtest-net-${n%%:*}" >/dev/null; done
    local leases=()
    for i in 0 1 2; do leases+=("--dhcp-host=02:42:00:00:0a:1$((1 + i)),$P.10.$((11 + i))"); done
    docker run -d --name mtest-net-dhcp --network mtest-net-lom --ip "$P.10.2" --cap-add NET_ADMIN mtest-e2e-dhcp \
        "--dhcp-range=$P.10.100,$P.10.200,255.255.255.0,1h" "--dhcp-option=3,$P.10.1" "${leases[@]}" >/dev/null
    for i in 0 1 2; do
        local h=${HOSTS[$i]} n=$((11 + i))
        docker run -d --name "$h" --hostname "$h" --privileged --cgroupns=host -e container=docker \
            -v /sys/fs/cgroup:/sys/fs/cgroup:rw --network mtest-net-lom --ip "$P.10.$n" \
            --mac-address "02:42:00:00:0a:1$((1 + i))" "$IMAGE" >/dev/null
        for net in "${NETS[@]:1}"; do
            docker network connect --ip "$P.${net#*:}.$n" "mtest-net-${net%%:*}" "$h"
        done
        docker cp "$ROOT/id_ed25519.pub" "$h:/root/.ssh/authorized_keys"
        docker cp "$HERE/fake_sysfs.py" "$h:/root/fake_sysfs.py"
        on "$h" "chown root:root /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys"
        # The installed system: LOM on DHCP, managed by networkd since boot.
        on "$h" "printf 'network:\n  version: 2\n  ethernets:\n    lom:\n      match: {name: eth0}\n      dhcp4: true\n' > /etc/netplan/50-lom.yaml && chmod 600 /etc/netplan/50-lom.yaml"
    done
    sleep 3
    for h in "${HOSTS[@]}"; do
        on "$h" "netplan apply 2>/dev/null; for _ in \$(seq 30); do ip route show default proto dhcp | grep -q . && break; sleep 1; done"
        # Docker's own default route was added before networkd ran; a booted host would not have it.
        on "$h" "ip route del default via $P.10.1 dev eth0 proto boot 2>/dev/null; ip route show default | grep -v 'proto dhcp' | while read -r r; do ip route del \$r; done; true"
    done
    for h in "${HOSTS[@]}"; do on "$h" "sed -i 's/172\.28\./$P./g' /root/fake_sysfs.py && python3 /root/fake_sysfs.py /fake-sys" >/dev/null; done
    printf 'images: {sb_worker: "x"}\nruntime: {results_dir: ./runs}\n' > "$ROOT/tests.yaml"
    cluster cluster.yaml "$P.20.1"
    cluster bad-gw.yaml "$P.20.254"
}

trap cleanup EXIT
setup

log "1. nic discover"
out=$(mtest cluster.yaml nic discover 2>&1) && rc=0 || rc=$?
check "discover exits 0" [ "$rc" = 0 ]
check "mgmt port found" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "d['mgmt_if']")" = eth1 ]
check "4 rails in PCI order" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "','.join(d['ib_devs'])")" = mlx5_0,mlx5_1,mlx5_2,mlx5_3 ]
check "LOM gateway found (DHCP)" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['lom']['gateway']")" = "$P.10.1" ]
check "golden match" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['checks']['golden_match']['result']")" = pass ]
out=$(mtest cluster.yaml nic show)
check "nic show prints rails" grep -q "mlx5_3" <<<"$out"

log "2. net apply --dry-run"
out=$(mtest cluster.yaml net apply --dry-run 2>&1) && rc=0 || rc=$?
check "dry-run exits 0" [ "$rc" = 0 ]
check "dry-run shows the netplan diff" grep -q "+    mtest-mgmt:" <<<"$out"
check "dry-run changes nothing" on gb300-r1-t01 "[ ! -e /etc/netplan/60-mtest.yaml ]"

log "3. net apply"
mtest cluster.yaml net apply >"$ROOT/apply.log" 2>&1 && rc=0 || rc=$?
cat "$ROOT/apply.log" | sed 's/^/     /' | tail -4
check "apply exits 0" [ "$rc" = 0 ]
check "every host passed" [ "$(state gb300-r1-t01 network)$(state gb300-r1-t02 network)$(state gb300-r2-t01 network)" = passpasspass ]
check "default route via mgmt only" [ "$(on gb300-r1-t02 "ip route show default")" = "default via $P.20.1 dev eth1 proto static " ]
check "LOM pin route merged into the LOM entry" on gb300-r1-t02 "ip route show 10.99.0.0/16 | grep -q 'via $P.10.1 dev eth0'"
check "LOM keeps its DHCP address" on gb300-r1-t02 "ip -4 addr show eth0 | grep -q 'inet $P.10.12/24.*dynamic'"
check "MTU on storage and rail" [ "$(on gb300-r2-t01 "cat /sys/class/net/eth2/mtu /sys/class/net/eth6/mtu" | tr '\n' ' ')" = "1450 1300 " ]
check "rollback timer stopped" on gb300-r1-t01 "! systemctl is-active --quiet mtest-net-rollback.timer"
check "peer pings recorded" grep -q '"ping:ib3-peer"' "$ROOT/runs/$(ls "$ROOT/runs" | grep -E '^[0-9]{8}-' | sort | tail -1)/gb300-r1-t01/network/summary.json"

log "4. nic discover + net apply again (idempotent)"
mtest cluster.yaml nic discover >/dev/null 2>&1 || true
check "LOM gateway still known after S4 removed the LOM default route" \
    [ "$(json "$ROOT/runs/nic/gb300-r1-t01.json" "d['lom']['gateway']")" = "$P.10.1" ]
out=$(mtest cluster.yaml net apply 2>&1) && rc=0 || rc=$?
check "second apply exits 0" [ "$rc" = 0 ]
check "reports no change" [ "$(grep -c 'no change' <<<"$out")" = 3 ]

log "5. bad gateway -> automatic rollback"
before=$(on gb300-r2-t01 "md5sum /etc/netplan/60-mtest.yaml")
start=$(date +%s)
mtest bad-gw.yaml net apply --hosts gb300-r2-t01 >"$ROOT/rollback.log" 2>&1 && rc=0 || rc=$?
took=$(( $(date +%s) - start ))
check "apply exits 1" [ "$rc" = 1 ]
check "status is fail with rollback reason" grep -q "rolled back" "$ROOT/rollback.log"
check "old configuration restored" grep -q "old configuration restored" "$ROOT/rollback.log"
check "file content restored" [ "$(on gb300-r2-t01 "md5sum /etc/netplan/60-mtest.yaml")" = "$before" ]
check "default route back to the good gateway" [ "$(on gb300-r2-t01 "ip route show default")" = "default via $P.20.1 dev eth1 proto static " ]
check "finished within rollback window (${took}s < 120s)" [ "$took" -lt 120 ]

log "6. net verify"
out=$(mtest cluster.yaml net verify 2>&1) && rc=0 || rc=$?
check "verify exits 0 after rollback" [ "$rc" = 0 ]

log "7. conflicting netplan entry is refused"
on gb300-r1-t02 "printf 'network:\n  version: 2\n  ethernets:\n    other:\n      match: {name: eth1}\n      dhcp4: true\n' > /etc/netplan/55-other.yaml && chmod 600 /etc/netplan/55-other.yaml"
out=$(mtest cluster.yaml net apply --hosts gb300-r1-t02 2>&1) && rc=0 || rc=$?
check "apply refused" [ "$rc" = 1 ]
check "reason names the file" grep -q "55-other.yaml: ethernets.other" <<<"$out"
on gb300-r1-t02 "rm -f /etc/netplan/55-other.yaml"

log "8. report"
mtest cluster.yaml report >/dev/null
check "report has S3 and S4" python3 -c "
import json,re,sys; s=open('$ROOT/runs/report/report.html').read()
d=json.loads(re.search(r'const D = (.*);\n', s).group(1).replace('<\\\\/', '</'))
sys.exit(0 if {'nic','network'} <= {i['id'] for i in d['items']} else 1)"

echo
log "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
