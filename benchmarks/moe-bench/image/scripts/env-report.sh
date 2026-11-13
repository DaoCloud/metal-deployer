#!/bin/bash
# Print the software and hardware facts that a MoE test result depends on.
set -uo pipefail

section() { printf '\n===== %s =====\n' "$1"; }

section "image"
cat /etc/moe-bench-build.info 2>/dev/null || echo "build info: missing"
echo "arch: $(uname -m)"
echo "kernel: $(uname -r)"

section "gpu"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,driver_version,compute_cap,memory.total --format=csv
    caps=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | sort -u | tr '\n' ' ')
    case "${caps}" in
        "10.3 ") ;;
        *) echo "WARN: compute capability is '${caps}', image is built for TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST:-?}" ;;
    esac
    section "gpu topology"
    nvidia-smi topo -m || true
    section "nvlink fabric (MNNVL / IMEX)"
    nvidia-smi -q | grep -A 4 -E '^\s*Fabric' || echo "no fabric information"
else
    echo "WARN: nvidia-smi not found. Start the container with --gpus all or a GPU device plugin."
fi

section "python stack"
python - <<'PY'
import torch
print("torch", torch.__version__)
print("torch cuda", torch.version.cuda)
try:
    v = torch.cuda.nccl.version()
    print("torch nccl", ".".join(map(str, v)) if isinstance(v, tuple) else v)
except Exception as e:
    print("torch nccl: unavailable", e)
print("cuda available", torch.cuda.is_available(), "devices", torch.cuda.device_count())
for name in ("deep_ep", "deep_ep_cpp", "hybrid_ep_cpp", "deep_gemm", "moonep", "cutlass"):
    try:
        m = __import__(name)
        print(name, getattr(m, "__version__", ""), m.__file__)
    except Exception as e:
        print(name, "IMPORT FAILED:", e)
from importlib import metadata
for dist in ("nvidia-nvshmem-cu13", "nvidia-cutlass-dsl"):
    try:
        print(dist, metadata.version(dist))
    except metadata.PackageNotFoundError:
        print(dist, "not installed")
PY
/opt/flashinfer-venv/bin/python - <<'PY' 2>&1 || echo "flashinfer venv: IMPORT FAILED"
from importlib import metadata
import flashinfer
print("flashinfer (venv)", flashinfer.__version__, flashinfer.__file__)
print("nvidia-cutlass-dsl (venv)", metadata.version("nvidia-cutlass-dsl"))
PY
nvcc --version | tail -n 2

section "rdma"
ls /dev/infiniband 2>/dev/null || echo "WARN: /dev/infiniband not present, inter-node RDMA tests cannot run"
command -v ibv_devinfo >/dev/null 2>&1 && ibv_devinfo -l 2>/dev/null
[ -e /dev/gdrdrv ] && echo "/dev/gdrdrv present" || echo "INFO: /dev/gdrdrv not present"
lsmod 2>/dev/null | grep -E '^(nvidia_peermem|gdrdrv) ' || true

section "launch environment"
for v in WORLD_SIZE RANK MASTER_ADDR MASTER_PORT NUM_OF_HYBRID_EP_RANKS_PER_NVLINK_DOMAIN \
         DEEPEP_ALLOW_MNNVL DEEPEP_LL_NUM_EXPERTS MOONEP_MEM_HANDLE_TYPE \
         FI_ONE_SIDED_BENCH_ARGS MEGA_MOE_NUM_EXPERTS MEGA_MOE_ARGS CUDA_VISIBLE_DEVICES; do
    echo "${v}=${!v:-}"
done
env | grep -E '^(NCCL|UCX|NVSHMEM)_' | sort || true
