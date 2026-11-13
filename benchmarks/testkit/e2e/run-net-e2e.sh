#!/bin/bash
# End-to-end test of S3 (testkit nic discover) and testkit net-check without GPU hosts.
#
# Starts three hosts as systemd containers (netplan, systemd-networkd, sshd) on seven
# docker networks: LOM (ssh), mgmt, storage and four "rails". Each host's network is
# configured with netplan the way the site script does it (default route via mgmt, LOM
# policy routing, storage route, IPoIB MTU); testkit itself never changes it. A fake /sys
# inside each host maps the veth interfaces to BlueField-3 and ConnectX-8 PCI devices.
# testkit then reads the hosts from network.yaml over real ssh: validate, nic discover
# (ports found by PCI, rails, golden compare, serial), net-check (pass, and a wrong MTU
# and PCI address that must fail) and the report.
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
testkit() {   # testkit NETWORK_FILE ARGS...
    local net=$1
    shift
    TESTKIT_SSH_CONFIG="$ROOT/ssh_config" TESTKIT_NODE_SYSFS=/fake-sys PYTHONPATH="$TESTKIT" \
        python3 -m testkit -n "$ROOT/$net" -t "$ROOT/tests.yaml" "$@"
}
on() { docker exec "$1" sh -c "$2"; }
json() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }

netplan_of() {   # netplan_of N: the site configuration of host .N
    local n=$1
    cat <<EOF
network:
  version: 2
  ethernets:
    lom:
      match: {name: eth0}
      addresses: [$P.10.$n/24]
      routes:
        - {to: $P.10.0/24, scope: link, table: 100}
        - {to: default, via: $P.10.1, table: 100}
      routing-policy:
        - {from: $P.10.$n/32, table: 100, priority: 100}
    mgmt:
      match: {name: eth1}
      addresses: [$P.20.$n/24]
      routes: [{to: default, via: $P.20.1}]
    storage:
      match: {name: eth2}
      addresses: [$P.30.$n/24]
      routes: [{to: 10.98.0.0/16, via: $P.30.1}]
EOF
    for k in 0 1 2 3; do
        printf '    rail%s:\n      match: {name: eth%s}\n      addresses: [%s]\n      mtu: 1300\n' \
            "$k" "$((3 + k))" "$P.4$k.$n/24"
    done
}

network() {   # network FILE [COMPUTE_MTU_ON_T02] [STORAGE_PCI_ON_T02]
    {
        echo "version: 1"
        echo "hosts:"
        for i in 0 1 2; do
            local n=$((11 + i)) h=${HOSTS[$i]} mtu=1300 spci=0009:01:00.1
            [ "$h" = gb300-r1-t02 ] && mtu=${2:-1300} && spci=${3:-0009:01:00.1}
            cat <<EOF
  $h:
    rack: ${RACKS[$i]}
    tray: ${TRAYS[$i]}
    lom: {ip: $P.10.$n/24, gateway: $P.10.1, policy: {table: 100, priority: 100}}
    mgmt: {pci: "0009:01:00.0", ip: $P.20.$n/24, mtu: 1500, gateway: $P.20.1, default_route: true}
    storage:
      pci: "$spci"
      ip: $P.30.$n/24
      routes: [{to: 10.98.0.0/16, via: $P.30.1}]
    compute:
      link_layer: infiniband
      mtu: $mtu
      devices:
EOF
            for k in 0 1 2 3; do echo "        - {pci: \"000$k:03:00.0\", ip: $P.4$k.$n/24}"; done
        done
    } > "$ROOT/$1"
}

setup() {
    log "building image"
    docker build -q --build-arg "BASE=${UBUNTU_IMAGE:-ubuntu:24.04}" -t "$IMAGE" -f "$HERE/sdnode.Dockerfile" "$HERE" >/dev/null
    cleanup
    mkdir -p "$ROOT"
    ssh-keygen -q -t ed25519 -N "" -f "$ROOT/id_ed25519"
    # testkit has no ssh settings of its own; the key comes from an extra ssh config file.
    printf 'Host *\n  IdentityFile %s\n  UserKnownHostsFile %s\n  ConnectTimeout 5\n' \
        "$ROOT/id_ed25519" "$ROOT/known_hosts" > "$ROOT/ssh_config"
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
        netplan_of "$n" > "$ROOT/netplan-$h.yaml"
        docker cp "$ROOT/netplan-$h.yaml" "$h:/etc/netplan/60-site.yaml"
        on "$h" "chmod 600 /etc/netplan/60-site.yaml"
    done
    sleep 3
    for h in "${HOSTS[@]}"; do
        # Docker's own default route was added before networkd ran; a booted host would not have it.
        on "$h" "ip route show default | while read -r r; do ip route del \$r; done; netplan apply 2>/dev/null; for _ in \$(seq 30); do ip route show default proto static | grep -q . && ip rule show | grep -q 'lookup 100' && break; sleep 1; done"
        on "$h" "sed -i 's/172\.28\./$P./g' /root/fake_sysfs.py && python3 /root/fake_sysfs.py /fake-sys" >/dev/null
    done
    cat > "$ROOT/tests.yaml" <<EOF
platform: gb300
golden: gb300-r1-t01
nic:
  expect: {gpus: 0, cx8: {count: 4, link_layer: InfiniBand, rate_gbps: 800}, bf3: {pf_count: 2, mode: nic}, compare_golden: true}
images: {sb_worker: "x"}
runtime: {results_dir: ./runs}
EOF
    network network.yaml
    network bad-network.yaml 4092 0009:01:00.7
}

trap cleanup EXIT
setup

log "0. validate"
out=$(testkit network.yaml validate --ssh 2>&1) && rc=0 || rc=$?
check "validate exits 0" [ "$rc" = 0 ]
check "hosts, racks and golden from network.yaml + tests.yaml" grep -q "3 hosts (R1 2, R2 1), golden gb300-r1-t01" <<<"$out"
check "all hosts reachable with TESTKIT_SSH_CONFIG" grep -q "ssh: 3/3 hosts reachable" <<<"$out"

log "1. nic discover"
out=$(testkit network.yaml nic discover 2>&1) && rc=0 || rc=$?
check "discover exits 0" [ "$rc" = 0 ]
check "mgmt port found by its PCI address" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "d['mgmt_if']")" = eth1 ]
check "storage port found by its PCI address" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "d['storage_if']")" = eth2 ]
check "4 rails in PCI order" [ "$(json "$ROOT/runs/nic/gb300-r1-t02.json" "','.join(d['ib_devs'])")" = mlx5_0,mlx5_1,mlx5_2,mlx5_3 ]
check "LOM gateway found in the policy table" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['lom']['gateway']")" = "$P.10.1" ]
check "golden match" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['checks']['golden_match']['result']")" = pass ]
check "network.yaml PCI found on host" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['checks']['plan_pci']['result']")" = pass ]
check "serial read from the host" [ "$(json "$ROOT/runs/nic/gb300-r2-t01.json" "d['serial']")" = SN-gb300-r2-t01 ]
out=$(testkit network.yaml nic show)
check "nic show prints rails" grep -q "mlx5_3" <<<"$out"
out=$(testkit bad-network.yaml nic discover --hosts gb300-r1-t02 2>&1) && rc=0 || rc=$?
check "discover fails on a PCI address that is not on the host" [ "$rc" = 1 ]
check "reason names the PCI" grep -q "storage PCI 0009:01:00.7 has no network interface" <<<"$out"
testkit network.yaml nic discover --hosts gb300-r1-t02 >/dev/null 2>&1 || true

log "2. net-check"
out=$(testkit network.yaml net-check 2>&1) && rc=0 || rc=$?
echo "$out" | tail -1 | sed 's/^/     /'
check "net-check exits 0 on the configured network" [ "$rc" = 0 ]
check "default route via mgmt" grep -q "gb300-r1-t02 .*default_route .*PASS" <<<"$out"
check "LOM policy routing" grep -q "gb300-r1-t01 .*lom_policy .*PASS" <<<"$out"
check "replies from the LOM address leave via the LOM" grep -q "gb300-r1-t01 .*lom_reply .*PASS" <<<"$out"
check "storage route" grep -q "gb300-r2-t01 .*route:storage 10.98.0.0/16 .*PASS" <<<"$out"
check "IPoIB MTU" grep -q "gb300-r2-t01 .*rail3 .*mtu 1300" <<<"$out"
check "same rail of the rack peer pinged" grep -q "gb300-r1-t01 .*ping:rail3-peer .*PASS" <<<"$out"
out=$(testkit bad-network.yaml net-check 2>&1) && rc=0 || rc=$?
check "net-check exits 1 on a mismatch" [ "$rc" = 1 ]
check "reports the wrong MTU" grep -q "gb300-r1-t02 .*rail0 .*mtu 1300 (expected 4092)" <<<"$out"
check "reports the missing PCI" grep -q "gb300-r1-t02 .*storage .*PCI 0009:01:00.7 not found" <<<"$out"
check "differences from most hosts are warnings" grep -q "warning: compute.mtu: gb300-r1-t02 4092 differs" <<<"$out"
check "nothing was changed on the hosts" [ "$(on gb300-r1-t02 "cat /sys/class/net/eth3/mtu; ls /etc/netplan")" = "1300
60-site.yaml" ]

log "3. report"
testkit network.yaml report >/dev/null
check "report has S3 and the serial" python3 -c "
import json,re,sys; s=open('$ROOT/runs/report/report.html').read()
d=json.loads(re.search(r'const D = (.*);\n', s).group(1).replace('<\\\\/', '</'))
ok = 'nic' in {i['id'] for i in d['items']} and {h['serial'] for h in d['hosts']} == {'SN-' + h['name'] for h in d['hosts']}
sys.exit(0 if ok else 1)"

echo
log "passed $PASSED, failed $FAILED"
[ "$FAILED" = 0 ]
