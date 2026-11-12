# syntax=docker/dockerfile:1.7
#
# MoE communication and compute test image for GB300 / B300 (Blackwell Ultra, sm_103, arm64/SBSA).
# Contents: DeepEP hybrid-ep branch (HybridEP + DeepEP V1 with NVSHMEM), MoonEP and DeepGEMM.

# NGC PyTorch 26.03: Ubuntu 24.04, Python 3.12, CUDA 13.2.0, PyTorch 2.11.0a0, NCCL 2.29.7.
# The digest is the linux/arm64 manifest of nvcr.io/nvidia/pytorch:26.03-py3.
ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:26.03-py3@sha256:9b1b9d6477afce05a5e941a3711093366b43de1b4eb7c1963251ade6a07c34fe

FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG GITHUB_URL=https://github.com
# deepseek-ai/DeepEP branch hybrid-ep, 2026-10-16.
ARG DEEPEP_REF=4e890fd19a3a52f23cc7bd38a0d9b0b1276f70d6
# deepseek-ai/DeepGEMM main, 2026-10-20.
ARG DEEPGEMM_REF=5081f8513af57e61696de4f4f04b0e707ffb503e
# rdma-core v60.0, required by the HybridEP RDMA (DOCA) path.
ARG RDMA_CORE_REF=89ddad4729501d5ad56815799aec012dab4f624d
# NVSHMEM for the DeepEP V1 internode and low-latency kernels (DeepEP needs >= 3.3.9).
# The base image ships NVSHMEM 3.5.21 (libnvshmem3-cuda-13 deb, linked by libtorch_cuda) without
# libnvshmem_device.a. The wheel supplies the device library and must be the same version,
# because both host libraries have the soname libnvshmem_host.so.3 and torch loads its copy first.
ARG NVSHMEM_VERSION=3.5.21
# MoonshotAI/MoonEP master, 2026-10-12.
ARG MOONEP_REF=d86fda40329de4002e0cade6817df7b4b5512ee5
# MoonEP setup.py pins nvidia-cutlass-dsl 4.6.2. The cu13 extra selects the CUDA 13 libraries.
ARG CUTLASS_DSL_VERSION=4.6.2
# GB300 and B300 are sm_103. Use "10.0 10.3" to also cover B200/GB200.
ARG TORCH_CUDA_ARCH_LIST=10.3
ARG MAX_JOBS=16

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build pkg-config git \
        libudev-dev libnl-3-dev libnl-route-3-dev \
        numactl libnuma-dev \
        infiniband-diags ibverbs-utils \
    && rm -rf /var/lib/apt/lists/*

# rdma-core v60.0 is built in place. HybridEP links libmlx5/libibverbs from this tree
# and records the directory in the extension rpath.
RUN git init /opt/rdma-core \
    && cd /opt/rdma-core \
    && git remote add origin ${GITHUB_URL}/linux-rdma/rdma-core.git \
    && git fetch --depth 1 origin ${RDMA_CORE_REF} \
    && git checkout --detach FETCH_HEAD \
    && EXTRA_CMAKE_FLAGS="-DNO_MAN_PAGES=1 -DNO_PYVERBS=1" bash build.sh \
    && test -e /opt/rdma-core/build/lib/libmlx5.so \
    && test -e /opt/rdma-core/build/lib/libibverbs.so

ENV RDMA_CORE_HOME=/opt/rdma-core/build \
    TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} \
    HYBRID_EP_MULTINODE=1 \
    USE_NIXL=0

# HybridEP links libnvidia-ml.so.1 and the extensions link libcuda.so.1. The NVIDIA
# container toolkit injects the real libraries at run time. The build uses the CUDA
# stubs from a private directory that is removed after the import check.
RUN mkdir -p /tmp/cuda-stub \
    && ln -s /usr/local/cuda/lib64/stubs/libnvidia-ml.so /tmp/cuda-stub/libnvidia-ml.so.1 \
    && ln -s /usr/local/cuda/lib64/stubs/libcuda.so /tmp/cuda-stub/libcuda.so.1

# Keep the base image torch and numpy when installing extra Python packages.
RUN pip list --format=freeze | grep -iE '^(torch|numpy)==' > /etc/moe-bench-constraints.txt \
    && cat /etc/moe-bench-constraints.txt

# NVSHMEM wheel. DeepEP setup.py finds it through the nvidia.nvshmem module and records
# its lib directory in the deep_ep_cpp rpath. HybridEP (hybrid_ep_cpp) does not use NVSHMEM.
RUN sys_ver=$(dpkg-query -W -f='${Version}' libnvshmem3-cuda-13 2>/dev/null | cut -d- -f1) \
    && if [ -n "${sys_ver}" ] && [ "${sys_ver}" != "${NVSHMEM_VERSION}" ]; then \
           echo "base image NVSHMEM ${sys_ver} != NVSHMEM_VERSION ${NVSHMEM_VERSION}" >&2; exit 1; \
       fi \
    && pip install -c /etc/moe-bench-constraints.txt "nvidia-nvshmem-cu13==${NVSHMEM_VERSION}"

# NVSHMEM perftests (shmem_put_bw, alltoall_latency, ...) for the optional testkit nvshmem items.
# They ship in libnvshmem3-dev-cuda-13 (/usr/bin/nvshmem_13/perftest), same version as the base
# image's libnvshmem3-cuda-13.
RUN arch=$(dpkg --print-architecture) \
    && case "${arch}" in arm64) repo=sbsa ;; amd64) repo=x86_64 ;; *) echo "unsupported ${arch}" >&2; exit 1 ;; esac \
    && deb=libnvshmem3-dev-cuda-13_${NVSHMEM_VERSION}-1_${arch}.deb \
    && curl -fsSL -o /tmp/${deb} https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/${repo}/${deb} \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/${deb} \
    && rm -rf /var/lib/apt/lists/* /tmp/${deb} \
    && test -x /usr/bin/nvshmem_13/perftest/device/pt-to-pt/shmem_put_bw

# DeepEP hybrid-ep. setup.py builds two extensions: hybrid_ep_cpp (HybridEP, builds the pinned
# NCCL submodule to get the DOCA GPUNetIO objects) and deep_ep_cpp (DeepEP V1, built with NVSHMEM).
# NVSHMEM 3.5 headers include <cuda/std/tuple>; CUDA 13 keeps CCCL under include/cccl, which the
# host compiler does not search by default. The patch makes DeepEP V1 build and run with NVSHMEM 3.5.
COPY patches/ /opt/moe-bench-patches/
RUN git clone --filter=blob:none ${GITHUB_URL}/deepseek-ai/DeepEP.git /opt/DeepEP \
    && cd /opt/DeepEP \
    && git checkout --detach ${DEEPEP_REF} \
    && git submodule update --init --recursive \
    && git apply -v /opt/moe-bench-patches/deepep-nvshmem-3.5.patch \
    && CPATH=/usr/local/cuda/include/cccl${CPATH:+:${CPATH}} \
       LIBRARY_PATH=/tmp/cuda-stub:${LIBRARY_PATH:-} MAX_JOBS=${MAX_JOBS} \
       pip install --no-build-isolation --no-deps -v . \
    && rm -rf /opt/DeepEP/build

# Fail the build if DeepEP V1 was compiled without NVSHMEM (internode and low-latency disabled).
RUN so=$(python -c "import importlib.util; print(importlib.util.find_spec('deep_ep_cpp').origin)") \
    && echo "deep_ep_cpp: ${so}" \
    && readelf -d "${so}" | grep -q 'libnvshmem_host' \
    || { echo "deep_ep_cpp is not linked with NVSHMEM" >&2; exit 1; }

# DeepGEMM. DG_FORCE_BUILD=1 builds from source instead of downloading a prebuilt wheel.
# DeepJIT needs elfutils/libdwfl.h at build time and loads libdw with dlopen for error backtraces.
RUN apt-get update && apt-get install -y --no-install-recommends libdw-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none ${GITHUB_URL}/deepseek-ai/DeepGEMM.git /opt/DeepGEMM \
    && cd /opt/DeepGEMM \
    && git checkout --detach ${DEEPGEMM_REF} \
    && git submodule update --init --recursive \
    && DG_FORCE_BUILD=1 MAX_JOBS=${MAX_JOBS} pip install --no-build-isolation --no-deps -v . \
    && rm -rf /opt/DeepGEMM/build

# MoonEP. The CUDA extension links libcuda; the kernels are CuTe DSL and compile at run time.
# The source tree stays in /opt/MoonEP because the upstream tests and benchmarks run from it.
RUN pip install -c /etc/moe-bench-constraints.txt "nvidia-cutlass-dsl[cu13]==${CUTLASS_DSL_VERSION}" \
    && git clone --filter=blob:none ${GITHUB_URL}/MoonshotAI/MoonEP.git /opt/MoonEP \
    && cd /opt/MoonEP \
    && git checkout --detach ${MOONEP_REF} \
    && LIBRARY_PATH=/tmp/cuda-stub:${LIBRARY_PATH:-} MAX_JOBS=${MAX_JOBS} \
       pip install --no-build-isolation --no-deps -v . \
    && rm -rf /opt/MoonEP/build

# Import check without a GPU. Only this step uses the stub libraries.
RUN cd / && LD_LIBRARY_PATH=/tmp/cuda-stub:${LD_LIBRARY_PATH:-} python - <<'PY'
import deep_ep, deep_ep_cpp, hybrid_ep_cpp, deep_gemm, moonep, cutlass
print("deep_ep", deep_ep.__file__)
print("deep_ep_cpp", deep_ep_cpp.__file__)
print("hybrid_ep_cpp", hybrid_ep_cpp.__file__)
print("deep_gemm", getattr(deep_gemm, "__version__", "?"), deep_gemm.__file__)
print("moonep", moonep.__file__)
print("cutlass", getattr(cutlass, "__version__", "?"))
PY
RUN rm -rf /tmp/cuda-stub

RUN printf '%s\n' \
      "base_image=${BASE_IMAGE}" \
      "deepep_ref=${DEEPEP_REF}" \
      "deepgemm_ref=${DEEPGEMM_REF}" \
      "rdma_core_ref=${RDMA_CORE_REF}" \
      "nvshmem_version=${NVSHMEM_VERSION}" \
      "moonep_ref=${MOONEP_REF}" \
      "cutlass_dsl_version=${CUTLASS_DSL_VERSION}" \
      "torch_cuda_arch_list=${TORCH_CUDA_ARCH_LIST}" \
      > /etc/moe-bench-build.info

COPY scripts/ /opt/moe-bench/
RUN chmod +x /opt/moe-bench/*.sh \
    && ln -s /opt/moe-bench/run-tests.sh /usr/local/bin/moe-bench \
    && ln -s /opt/moe-bench/env-report.sh /usr/local/bin/moe-bench-env

WORKDIR /opt/moe-bench
CMD ["sleep", "infinity"]
