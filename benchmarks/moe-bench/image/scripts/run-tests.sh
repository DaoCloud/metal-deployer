#!/bin/bash
# Run the upstream HybridEP, DeepEP V1, MoonEP and DeepGEMM tests and collect one log per test.
#
# Multi-node: run the same command once per node, inside one container per node.
# Set WORLD_SIZE (number of nodes), RANK (node index, 0..WORLD_SIZE-1),
# MASTER_ADDR and MASTER_PORT on every node. Each test starts one process per local GPU.
set -uo pipefail

DEEPEP_TESTS=/opt/DeepEP/tests
DEEPGEMM_TESTS=/opt/DeepGEMM/tests
MOONEP_DIR=/opt/MoonEP

# Exit code of run_one for a test that does not apply to this topology.
SKIP_RC=77

HYBRID_EP_DEFAULT="hybrid_ep graphed_hybrid_ep ragged_hybrid_ep hybrid_ep_token_drop"
DEEPEP_DEFAULT="deepep_intranode deepep_internode deepep_low_latency"
MOONEP_DEFAULT="moonep_tests moonep_bench"
DEEPGEMM_DEFAULT="layout bf16 fp8_fp4"
ALL_TESTS="${HYBRID_EP_DEFAULT} ${DEEPEP_DEFAULT} ${MOONEP_DEFAULT} ${DEEPGEMM_DEFAULT} mega_moe"
MOONEP_TEST_FILES="test_api_validation test_planning test_dispatch test_combine test_e2e test_grad_reduce test_prefetch"

usage() {
    cat <<EOF
Usage: moe-bench [options]

Options:
  --suite NAME          hybrid-ep | deepep | moonep | deepgemm | all (default: all)
  --tests LIST          Comma-separated test names. Overrides --suite.
                        Names: ${ALL_TESTS// /, }
  --num-processes N     GPUs per node (default: number of visible GPUs)
  --only-bf16           Skip FP8 cases in hybrid_ep and ragged_hybrid_ep
  --log-dir DIR         Log directory (default: /tmp/moe-bench/<time>)
  --list                Print test names and exit
  -h, --help            Print this help

Default suites:
  hybrid-ep: ${HYBRID_EP_DEFAULT}
  deepep:    ${DEEPEP_DEFAULT}
  moonep:    ${MOONEP_DEFAULT}
  deepgemm:  ${DEEPGEMM_DEFAULT}
  all:       hybrid-ep + deepep + moonep + deepgemm
  mega_moe (DeepGEMM fused EP + GEMM) runs only when named in --tests.

A test that does not apply to the current topology is reported as SKIP, not FAIL:
  deepep_intranode   needs nodes x GPUs per node <= 8 (more than one node uses MNNVL)
  deepep_internode   needs 8 GPUs per node and at least 2 nodes (not GB300)

HybridEP test sizes come from environment variables, for example:
  HIDDEN_DIM, NUM_TOKENS_PER_RANK, MAX_NUM_OF_TOKENS_PER_RANK, NUM_LOCAL_EXPERTS, TOPK,
  NUM_SMS_DISPATCH, NUM_SMS_COMBINE, NUM_OF_HYBRID_EP_RANKS_PER_NVLINK_DOMAIN.
DeepEP V1:
  DEEPEP_ALLOW_MNNVL=1|0      Use NVLink across nodes when nodes > 1 (default: 1)
  DEEPEP_LL_NUM_EXPERTS=N     Experts for deepep_low_latency (default: smallest
                              multiple of the rank count that is >= 288)
MoonEP:
  MOONEP_MEM_HANDLE_TYPE=auto|fabric|fd   Upstream switch, auto uses fabric across nodes
  MOONEP_BENCH_ARGS="..."     Extra arguments for benchmarks/bench_comm.py
EOF
}

suite=all
tests=""
num_processes=""
only_bf16=0
log_dir=""

while [ $# -gt 0 ]; do
    case "$1" in
        --suite) suite="$2"; shift 2 ;;
        --tests) tests="${2//,/ }"; shift 2 ;;
        --num-processes) num_processes="$2"; shift 2 ;;
        --only-bf16) only_bf16=1; shift ;;
        --log-dir) log_dir="$2"; shift 2 ;;
        --list) echo "${ALL_TESTS}" | tr ' ' '\n'; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "${tests}" ]; then
    case "${suite}" in
        hybrid-ep) tests="${HYBRID_EP_DEFAULT}" ;;
        deepep) tests="${DEEPEP_DEFAULT}" ;;
        moonep) tests="${MOONEP_DEFAULT}" ;;
        deepgemm) tests="${DEEPGEMM_DEFAULT}" ;;
        all) tests="${HYBRID_EP_DEFAULT} ${DEEPEP_DEFAULT} ${MOONEP_DEFAULT} ${DEEPGEMM_DEFAULT}" ;;
        *) echo "unknown suite: ${suite}" >&2; exit 2 ;;
    esac
fi

if [ -z "${num_processes}" ]; then
    num_processes=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
    if [ "${num_processes}" -lt 1 ]; then
        echo "no GPU visible. Start the container with --gpus all." >&2
        exit 1
    fi
fi

world_size=${WORLD_SIZE:-1}
node_rank=${RANK:-0}
if [ "${world_size}" -gt 1 ] && [ -z "${MASTER_ADDR:-}" ]; then
    echo "WORLD_SIZE=${world_size} needs MASTER_ADDR (address of the node with RANK=0)." >&2
    exit 2
fi

log_dir=${log_dir:-/tmp/moe-bench/$(date +%Y%m%d-%H%M%S)}
mkdir -p "${log_dir}"
host=$(hostname)
prefix="${log_dir}/${host}-rank${node_rank}"

/opt/moe-bench/env-report.sh > "${prefix}-env.log" 2>&1
echo "environment report: ${prefix}-env.log"
echo "nodes=${world_size} node_rank=${node_rank} gpus_per_node=${num_processes}"

bf16_flag=()
[ "${only_bf16}" -eq 1 ] && bf16_flag=(--only-bf16)

total_ranks=$(( world_size * num_processes ))
mnnvl_flag=()
if [ "${world_size}" -gt 1 ] && [ "${DEEPEP_ALLOW_MNNVL:-1}" = "1" ]; then
    mnnvl_flag=(--allow-mnnvl)
fi

# torchrun launcher for MoonEP. It sets RANK/WORLD_SIZE/LOCAL_RANK per GPU process.
torchrun_cmd() {
    if [ "${world_size}" -gt 1 ]; then
        torchrun --nnodes "${world_size}" --node_rank "${node_rank}" \
            --nproc_per_node "${num_processes}" \
            --master_addr "${MASTER_ADDR}" --master_port "${MASTER_PORT:-29500}" "$@"
    else
        torchrun --standalone --nproc_per_node "${num_processes}" "$@"
    fi
}

skip() {
    echo "SKIP: $*"
    return "${SKIP_RC}"
}

run_deepep() {
    local name="$1"
    case "${name}" in
        deepep_intranode)
            # V1 normal mode treats at most 8 ranks as one NVLink group.
            [ "${total_ranks}" -le 8 ] \
                || skip "intranode needs nodes x GPUs <= 8, got ${world_size} x ${num_processes}" || return
            local fabric=()
            [ "${world_size}" -gt 1 ] && fabric=("${mnnvl_flag[@]}" --use-fabric)
            (cd "${DEEPEP_TESTS}" && python test_intranode.py --num-processes "${num_processes}" "${fabric[@]}") ;;
        deepep_internode)
            # V1 normal mode hardcodes 8 NVLink peers per node (NUM_MAX_NVL_PEERS); the test asserts it.
            [ "${num_processes}" -eq 8 ] \
                || skip "internode needs 8 GPUs per node, got ${num_processes}" || return
            [ "${world_size}" -gt 1 ] || skip "internode needs at least 2 nodes" || return
            (cd "${DEEPEP_TESTS}" && python test_internode.py --num-processes "${num_processes}") ;;
        deepep_low_latency)
            local experts=${DEEPEP_LL_NUM_EXPERTS:-$(( (288 + total_ranks - 1) / total_ranks * total_ranks ))}
            (cd "${DEEPEP_TESTS}" && python test_low_latency.py --num-processes "${num_processes}" \
                --num-experts "${experts}" "${mnnvl_flag[@]}") ;;
    esac
}

run_moonep() {
    local name="$1" rc=0 f
    case "${name}" in
        moonep_tests)
            for f in ${MOONEP_TEST_FILES}; do
                echo ">>> MoonEP tests/${f}.py"
                (cd "${MOONEP_DIR}" && torchrun_cmd -m pytest -q "tests/${f}.py") || rc=1
            done
            return "${rc}" ;;
        moonep_bench)
            # bench_comm.py imports tests.generate_topk_routing, so it runs from the source tree.
            # shellcheck disable=SC2086
            (cd "${MOONEP_DIR}" && PYTHONPATH="${MOONEP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
                torchrun_cmd benchmarks/bench_comm.py --out "${prefix}-moonep_bench.csv" ${MOONEP_BENCH_ARGS:-}) ;;
    esac
}

run_one() {
    local name="$1"
    case "${name}" in
        hybrid_ep|ragged_hybrid_ep)
            (cd "${DEEPEP_TESTS}" && python "test_${name}.py" --num-processes "${num_processes}" "${bf16_flag[@]}") ;;
        graphed_hybrid_ep|hybrid_ep_token_drop)
            (cd "${DEEPEP_TESTS}" && python "test_${name}.py" --num-processes "${num_processes}") ;;
        deepep_intranode|deepep_internode|deepep_low_latency)
            run_deepep "${name}" ;;
        moonep_tests|moonep_bench)
            run_moonep "${name}" ;;
        layout|bf16|fp8_fp4)
            if [ "${world_size}" -gt 1 ] && [ "${node_rank}" -ne 0 ]; then
                echo "single-GPU test, skipped on node rank ${node_rank}"
                return 0
            fi
            (cd "${DEEPGEMM_TESTS}" && python "test_${name}.py") ;;
        mega_moe)
            (cd "${DEEPGEMM_TESTS}" && python test_mega_moe.py --num-processes "${num_processes}") ;;
        *)
            echo "unknown test: ${name}" >&2
            return 2 ;;
    esac
}

declare -A result
failed=0
for t in ${tests}; do
    log="${prefix}-${t}.log"
    echo ">>> ${t} (log: ${log})"
    start=$(date +%s)
    run_one "${t}" > "${log}" 2>&1
    rc=$?
    secs=$(( $(date +%s) - start ))
    if [ "${rc}" -eq 0 ]; then
        result[${t}]="PASS ${secs}s"
    elif [ "${rc}" -eq "${SKIP_RC}" ]; then
        result[${t}]="SKIP $(grep -m1 '^SKIP: ' "${log}" | cut -c7-)"
    else
        result[${t}]="FAIL rc=${rc} ${secs}s"
        failed=1
        tail -n 20 "${log}"
    fi
    echo "<<< ${t}: ${result[${t}]}"
done

summary="${prefix}-summary.txt"
{
    echo "host=${host} node_rank=${node_rank} nodes=${world_size} gpus_per_node=${num_processes}"
    for t in ${tests}; do printf '%-24s %s\n' "${t}" "${result[${t}]}"; done
} | tee "${summary}"

exit "${failed}"
