#!/bin/sh
# S7 IMEX T1/T2, run by testkit as the mpi launcher inside the rank 0 container (SuperBench worker image).
# /testkit holds imex_fabric_ipc_test.c; /testkit-ssh/hostfile lists the group in rank order.
# Results go to /results/raw; testkit parses them. Exit 0 when the steps ran (pass/fail is judged by testkit).
#
# Env: TESTKIT_NNODES, TESTKIT_IMEX_T1 / TESTKIT_IMEX_T2 (1 = run), TESTKIT_IMEX_GPUS (GPUs per node),
#      TESTKIT_NVBANDWIDTH (path of nvbandwidth built with MULTINODE=1)
set -u
R=/results/raw
mkdir -p "$R"
HF=/testkit-ssh/hostfile
NN=${TESTKIT_NNODES:-$(grep -c . "$HF")}
GPN=${TESTKIT_IMEX_GPUS:-4}
NODES=$(awk '{print $1}' "$HF")
N0=$(echo "$NODES" | sed -n 1p)
N1=$(echo "$NODES" | sed -n 2p)
MPI="mpirun --allow-run-as-root --bind-to none"
ONE_PER_NODE=$(echo "$NODES" | sed 's/$/:1/' | paste -sd, -)
CUDA=${CUDA_HOME:-/usr/local/cuda}
MPICC=$(command -v mpicc || echo /opt/hpcx/ompi/bin/mpicc)

if [ "${TESTKIT_IMEX_T1:-1}" = 1 ]; then
    echo "== T1 build imex_fabric_ipc_test on every node"
    $MPI -np "$NN" -H "$ONE_PER_NODE" sh -c "$MPICC -O2 -o /tmp/imex_fabric_ipc_test /testkit/imex_fabric_ipc_test.c \
        -I$CUDA/include -L$CUDA/lib64 -L$CUDA/lib64/stubs -lcuda" > "$R/t1-build.log" 2>&1
    rc=$?
    echo $rc > "$R/t1-build.rc"
    if [ $rc = 0 ] && [ -n "$N1" ]; then
        echo "== T1 pair $N0 <-> $N1"
        $MPI -np 2 -H "$N0:1,$N1:1" /tmp/imex_fabric_ipc_test --size 256 --iters 20 > "$R/t1-pair.log" 2>&1
        echo $? > "$R/t1-pair.rc"
        echo "== T1 all $NN nodes"
        $MPI -np "$NN" -H "$ONE_PER_NODE" /tmp/imex_fabric_ipc_test --size 256 --iters 5 > "$R/t1-all.log" 2>&1
        echo $? > "$R/t1-all.rc"
    elif [ $rc != 0 ]; then
        echo "T1 build failed:"; tail -5 "$R/t1-build.log"
    fi
fi

if [ "${TESTKIT_IMEX_T2:-1}" = 1 ]; then
    NVB=${TESTKIT_NVBANDWIDTH:-/opt/superbench/bin/nvbandwidth}
    if [ -n "$N1" ] && [ -x "$NVB" ] && "$NVB" -l 2>/dev/null | grep -q multinode_device_to_device; then
        echo "== T2 nvbandwidth multinode $N0 + $N1"
        $MPI -np $((2 * GPN)) -H "$N0:$GPN,$N1:$GPN" "$NVB" \
            -t multinode_device_to_device_memcpy_read_ce multinode_device_to_device_memcpy_write_ce \
            > "$R/t2-nvbandwidth.log" 2>&1
        echo $? > "$R/t2-nvbandwidth.rc"
    else
        echo "$NVB has no multinode_* tests (needs nvbandwidth built with -DMULTINODE=1)" > "$R/t2-skip.txt"
        cat "$R/t2-skip.txt"
    fi
fi
echo "done"
exit 0
