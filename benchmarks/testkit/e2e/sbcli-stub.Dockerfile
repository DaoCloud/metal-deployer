# Stand-in for the sbcli image in the end-to-end test. "sb" is a stub: deploy checks that every
# host in the inventory accepts the run key over ssh and has the worker image; run writes results
# in SuperBench's results-summary.jsonl format. The real sb needs GPU hosts.
FROM mtest-e2e-worker:latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-yaml \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /workspace \
    && printf 'superbench:\n  enable: [cublaslt-gemm:compute, gpu-burn, nccl-bw:bw:allreduce:nvlink]\n' \
       > /workspace/config_gb300_single_node.yaml \
    && printf 'superbench:\n  enable: [ib-traffic:perftest:multinode, ib-traffic:perftest:multinode-lat, nccl-bw:bw:allreduce:ib]\n' \
       > /workspace/config_gb300_multi_node.yaml \
    && printf 'superbench:\n  enable: [nccl-bw:bw:allreduce:mnnvl]\n' > /workspace/config_gb300_nvl_domain.yaml \
    && printf 'superbench:\n  enable: [nccl-bw:bw:allreduce:mnnvl-ib]\n' > /workspace/config_gb300_all_nodes.yaml \
    && printf 'superbench:\n  benchmarks:\n%s' "$(for o in allreduce reducescatter allgather; do for c in U-A U-B O-A O-B O-C O-D; do printf '    nccl-bw:sharp:%s:%s: {}\n' $o $c; done; done)" \
       > /workspace/config_gb300_sharp.yaml \
    && touch /workspace/rule_gb300.yaml
COPY sb-stub.py /usr/local/bin/sb
RUN chmod +x /usr/local/bin/sb
