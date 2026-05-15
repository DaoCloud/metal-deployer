#!/bin/bash
set -euo pipefail

apt-get update
apt-get install -y --no-install-recommends \
    infiniband-diags rdmacm-utils ibverbs-utils ibutils2 perftest \
    rdma-core libibverbs-dev librdmacm-dev libibumad-dev || true
