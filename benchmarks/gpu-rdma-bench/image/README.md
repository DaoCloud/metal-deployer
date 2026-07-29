# GPU/RDMA Bench Image Build Guide

Unified image for DeepEP, UCX perftest, and NIXLBench.

## Layout

```text
benchmarks/gpu-rdma-bench/image/
├── Dockerfile
├── scripts/
│   ├── build-deepep.sh
│   └── build-ucx.sh
└── tools/
    ├── run-nixlbench
    ├── testDeepEP
    └── testUcx
```

## Build

From repository root:

```bash
make build-gpu-rdma-bench
```

Override image tag:

```bash
make build-gpu-rdma-bench GPU_RDMA_BENCH_IMAGE_TAG=v0.1.0
```

Override component versions:

```bash
make build-gpu-rdma-bench \
  ENV_DEEPEP_VERSION=v1.2.1 \
  ENV_UCX_VERSION=v1.20.0 \
  ENV_GDRCOPY_VERSION=v2.5.1 \
  ENV_NIXL_REF=main \
  ENV_LIBFABRIC_VERSION=v1.21.0
```

Direct Docker build:

```bash
docker build \
  --build-arg ENV_NIXL_REF=main \
  --build-arg ENV_UCX_VERSION=v1.20.0 \
  --build-arg ENV_TORCH_CUDA_ARCH_LIST=9.0 \
  -t gpu-rdma-bench:latest \
  benchmarks/gpu-rdma-bench/image
```

## Build Notes

UCX is built with CUDA, verbs, rdmacm, and multi-thread support:

```bash
./contrib/configure-release --with-cuda=/usr/local/cuda --with-verbs --with-rdmacm --enable-mt --without-go
```

`--enable-mt` matters for NIXLBench threaded/progress-thread paths.

GDRCopy userspace library is built from source before DeepEP and libfabric so NIXL can enable GDRCopy dlopen support. Libfabric is built from source with CUDA and EFA enabled for NIXL libfabric backend.

DeepEP defaults to `ENV_TORCH_CUDA_ARCH_LIST=9.0` (Hopper/SM90). For Ampere (SM80, A100/A800) rebuild with `--build-arg ENV_TORCH_CUDA_ARCH_LIST="8.0"`; `build-deepep.sh` then sets `DISABLE_SM90_FEATURES=1` automatically (disables FP8/TMA/Hopper launch methods). SM80 and SM90 cannot be combined in one image because `DISABLE_SM90_FEATURES` is a global compile-time switch.

## Image

```text
ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest
```
