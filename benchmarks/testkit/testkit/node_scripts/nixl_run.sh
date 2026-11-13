#!/bin/bash
# NIXLBench cases (optional NIXL items), run by testkit as the mpi launcher inside the rank 0 container
# of the nixl-benchmark image. testkit writes the plan to /testkit:
#   nixl_cases.tsv  name, kind (local|pair|storage), CUDA device of initiator / target ("-": none),
#                   nixlbench arguments; tab separated
#   nixl_pairs.tsv  initiator mgmt IP, initiator host, target mgmt IP, target host (pair cases)
# Results go to /results/raw: <run>.txt (initiator output), <run>.target.txt, <run>.rc, where <run> is
# the case name, or <case>@<initiator>+<target> for pair cases. testkit parses them.
# Exit 0 when the cases ran (pass/fail is judged by testkit).
#
# Env: TESTKIT_MASTER_ADDR     mgmt IP of this host (etcd advertise address)
#      TESTKIT_NIXL_ETCD_PORT  etcd client port (default 12379; port + 1 is the peer port on 127.0.0.1)
#      TESTKIT_NIXL_TIMEOUT    seconds per nixlbench process (default 900)
#
# "nixl_run.sh exec <device> <timeout> <args...>" runs one nixlbench process with the environment of
# this container (an ssh session does not get the docker env), so per-host UCX_NET_DEVICES etc. apply.
# The mpi-mode entry saves that env to /run/testkit-env.sh before sshd starts.
set -u

if [ "${1:-}" = exec ]; then
    dev=$2
    t=$3
    shift 3
    if [ -r /run/testkit-env.sh ]; then
        # shellcheck disable=SC1091
        . /run/testkit-env.sh
    else
        # shellcheck disable=SC2163  # kv is "NAME=value"
        while IFS= read -r -d '' kv; do export "$kv"; done < /proc/1/environ
    fi
    if [ "$dev" != - ]; then export CUDA_VISIBLE_DEVICES="$dev"; fi
    exec timeout "$t" nixlbench "$@"
fi

R=/results/raw
mkdir -p "$R"
CASES=/testkit/nixl_cases.tsv
PAIRS=/testkit/nixl_pairs.tsv
SSH="ssh -n -F /testkit-ssh/config -o ConnectTimeout=10"
SELF=${TESTKIT_MASTER_ADDR:-127.0.0.1}
PORT=${TESTKIT_NIXL_ETCD_PORT:-12379}
T=${TESTKIT_NIXL_TIMEOUT:-900}
E=http://${SELF}:${PORT}
ETCD_PID=""

cleanup() {
    [ -n "$ETCD_PID" ] && kill "$ETCD_PID" 2>/dev/null
    pkill -x nixlbench 2>/dev/null
}
trap cleanup EXIT
trap 'echo "nixl: stopped"; exit 143' TERM INT

# on <ip> <command...>: run here when ip is this host, else over ssh in the peer container
on() {
    local ip=$1
    shift
    if [ "$ip" = "$SELF" ]; then
        "$@"
    else
        # shellcheck disable=SC2029
        $SSH "$ip" "$*"
    fi
}

start_etcd() {
    rm -rf /tmp/testkit-nixl-etcd
    etcd --name testkit --data-dir /tmp/testkit-nixl-etcd \
        --listen-client-urls "http://0.0.0.0:${PORT}" --advertise-client-urls "$E" \
        --listen-peer-urls "http://127.0.0.1:$((PORT + 1))" \
        --initial-advertise-peer-urls "http://127.0.0.1:$((PORT + 1))" \
        --initial-cluster "testkit=http://127.0.0.1:$((PORT + 1))" > "$R/etcd.log" 2>&1 &
    ETCD_PID=$!
    for _ in $(seq 1 30); do
        if ETCDCTL_API=3 etcdctl --endpoints="$E" endpoint health > /dev/null 2>&1; then
            echo "etcd: $E"
            return 0
        fi
        sleep 1
    done
    echo "etcd did not start on $E (see raw/etcd.log)"
    echo "etcd not healthy at $E" > "$R/etcd-failed"
    return 1
}

etcd_clean() {
    ETCDCTL_API=3 etcdctl --endpoints="$E" del "" --from-key=true > /dev/null 2>&1
}

# two <run> <initiator ip> <target ip> <dev initiator> <dev target> <args>: nixlbench rank 0 (initiator)
# and rank 1 (target); ranks are given in the order the processes register in etcd.
two() {
    local run=$1 ia=$2 ib=$3 di=$4 dt=$5 args=$6
    echo "== $run"
    etcd_clean
    on "$ia" pkill -x nixlbench > /dev/null 2>&1
    on "$ib" pkill -x nixlbench > /dev/null 2>&1
    # shellcheck disable=SC2086
    on "$ia" bash /testkit/nixl_run.sh exec "$di" "$T" $args --etcd_endpoints "$E" > "$R/$run.txt" 2>&1 &
    local pi=$!
    sleep 5
    # shellcheck disable=SC2086
    on "$ib" bash /testkit/nixl_run.sh exec "$dt" "$T" $args --etcd_endpoints "$E" > "$R/$run.target.txt" 2>&1 &
    local pt=$!
    wait "$pi"
    local rc=$?
    # The target finishes shortly after the initiator
    for _ in $(seq 1 30); do kill -0 "$pt" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pt" 2>/dev/null; then
        on "$ib" pkill -x nixlbench > /dev/null 2>&1
        kill "$pt" 2>/dev/null
    fi
    wait "$pt" 2>/dev/null
    echo "$rc" > "$R/$run.rc"
    tail -n 2 "$R/$run.txt"
}

[ -s "$CASES" ] || { echo "no cases in $CASES"; exit 0; }
if cut -f2 "$CASES" | grep -qv '^storage$'; then
    start_etcd || exit 0
fi

while IFS="$(printf '\t')" read -r name kind di dt args; do
    [ -n "$name" ] || continue
    case "$kind" in
        local)
            two "$name" "$SELF" "$SELF" "$di" "$dt" "$args" < /dev/null
            ;;
        pair)
            while IFS="$(printf '\t')" read -r ia ha ib hb; do
                [ -n "$ia" ] || continue
                two "$name@$ha+$hb" "$ia" "$ib" "$di" "$dt" "$args" < /dev/null
            done < "$PAIRS"
            ;;
        storage)
            echo "== $name"
            # shellcheck disable=SC2086
            bash /testkit/nixl_run.sh exec "$di" "$T" $args > "$R/$name.txt" 2>&1 < /dev/null
            echo $? > "$R/$name.rc"
            tail -n 2 "$R/$name.txt"
            ;;
        *)
            echo "$name: unknown kind $kind"
            ;;
    esac
done < "$CASES"
echo "done"
exit 0
