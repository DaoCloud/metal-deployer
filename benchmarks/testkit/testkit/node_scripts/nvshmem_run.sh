#!/bin/sh
# S7b NVSHMEM perftests (optional), run by testkit as the mpi launcher inside the rank 0 container.
# /testkit-ssh/hostfile lists the group in rank order. Results go to /results/raw/<test>.txt and .rc;
# testkit parses them. Exit 0 when the tests ran (pass/fail is judged by testkit).
#
# Env: TESTKIT_NVSHMEM_TESTS  space separated paths under TESTKIT_NVSHMEM_DIR, e.g. pt-to-pt/shmem_put_bw
#      TESTKIT_NVSHMEM_DIR    perftest directory (default /usr/bin/nvshmem_13/perftest/device)
#      TESTKIT_NVSHMEM_PPN    processes (GPUs) per node
#      TESTKIT_NVSHMEM_ARGS   extra arguments for every perftest
#      TESTKIT_NNODES         nodes in the group
set -u
R=/results/raw
mkdir -p "$R"
HF=/testkit-ssh/hostfile
NN=${TESTKIT_NNODES:-$(grep -c . "$HF")}
PPN=${TESTKIT_NVSHMEM_PPN:-1}
DIR=${TESTKIT_NVSHMEM_DIR:-/usr/bin/nvshmem_13/perftest/device}
ARCH=$(uname -m)
LIB=/usr/lib/${ARCH}-linux-gnu/nvshmem/13
export LD_LIBRARY_PATH="$LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export NVSHMEM_MACHINE_READABLE_OUTPUT=1
export NVSHMEM_BOOTSTRAP=${NVSHMEM_BOOTSTRAP:-MPI}
NP=$((NN * PPN))
MPI="mpirun --allow-run-as-root --bind-to none --hostfile $HF -np $NP --map-by ppr:$PPN:node \
 -x LD_LIBRARY_PATH -x NVSHMEM_MACHINE_READABLE_OUTPUT -x NVSHMEM_BOOTSTRAP"
for v in $(env | sed -n 's/^\(NVSHMEM_[A-Z0-9_]*\)=.*/\1/p'); do
    case "$v" in NVSHMEM_MACHINE_READABLE_OUTPUT|NVSHMEM_BOOTSTRAP) ;; *) MPI="$MPI -x $v" ;; esac
done
[ -d "$DIR" ] || { echo "$DIR not found (libnvshmem3-dev-cuda-13 in the image?)"; echo "missing" > "$R/perftest-missing"; exit 0; }
for t in ${TESTKIT_NVSHMEM_TESTS:-pt-to-pt/shmem_put_bw}; do
    name=$(basename "$t")
    echo "== $t ($NP processes, $PPN per node)"
    # shellcheck disable=SC2086
    $MPI "$DIR/$t" ${TESTKIT_NVSHMEM_ARGS:-} > "$R/$name.txt" 2>&1
    rc=$?
    echo $rc > "$R/$name.rc"
    tail -n 3 "$R/$name.txt"
done
echo "done"
exit 0
