# NIXL Benchmark 镜像

NIXL 与 NIXLBench 测试镜像：NIXL 插件 UCX、LIBFABRIC、GDS、GDS_MT、POSIX，外加 etcd（NIXLBench 进程汇合）和验收脚本 `testNixl`、`run-nixlbench`。

- 完整使用指南（testNixl S1-S6、结果解读、基线）：[docs/nixl-benchmark.md](../../docs/nixl-benchmark.md)
- S1-S6 测试项定义与判定标准：[nixlbench-acceptance-smoke-suite.md](./nixlbench-acceptance-smoke-suite.md)
- 在 mtest 中运行（`nixl.node`、`nixl.pair`、`nixl.storage`）：[benchmarks/testkit/README.md](../testkit/README.md#nixlnixlbench可选默认关闭)

## 镜像

每个 profile 一个 tag，tag 以 `-<profile>` 结尾：

| profile | 平台 | 适用 | 镜像 |
| --- | --- | --- | --- |
| `cuda13-gb300` | linux/arm64 | GB300（sm_103），与 `cuda13-gb300` ISO 同为 CUDA 13.0 | `ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300` |
| `cuda13-x86` | linux/amd64 | B200 / B300 x86 服务器（sm_100 / sm_103） | `ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-x86` |

发版 tag 为 `<version>-<profile>`，如 `0.1.0-cuda13-gb300`。镜像内 `/etc/nixl-benchmark-build.info` 记录 profile、NIXL 版本与 commit、各依赖版本。

## 构建

只有一个 [Dockerfile](./image/Dockerfile)，所有版本和开关都是 `ARG`。场景差异放在 [image/profiles/](./image/profiles/) 下的 `<profile>.env`（`KEY=VALUE`，只写与默认值不同的项）。[image/profile.sh](./image/profile.sh) 把 profile 转成 `--build-arg`，Makefile 和 CI 都用它：

```bash
make build-nixl-benchmark                              # 默认 NIXL_PROFILE=cuda13-gb300（非 arm64 主机需 QEMU binfmt）
make build-nixl-benchmark NIXL_PROFILE=cuda13-x86
make test-nixl-benchmark NIXL_PROFILE=cuda13-x86       # 打印构建信息、插件列表、nixlbench --help（无需 GPU）
```

与 Dockerfile `ARG` 同名的环境变量或 make 变量覆盖 profile 中的值：

```bash
# 换 NIXL 版本
make build-nixl-benchmark NIXL_PROFILE=cuda13-x86 NIXL_REF=v1.5.1
# Docker Hub 不通时用镜像站
BASE_BUILD_IMAGE=docker.m.daocloud.io/nvidia/cuda:13.0.2-devel-ubuntu24.04 \
BASE_RUNTIME_IMAGE=docker.m.daocloud.io/nvidia/cuda:13.0.2-runtime-ubuntu24.04 \
  make build-nixl-benchmark
# 查看某个 profile 解析后的构建参数
benchmarks/nixl-benchmark/image/profile.sh cuda13-gb300 args
```

主要参数：

| ARG | 默认值 | 说明 |
| --- | --- | --- |
| `BASE_BUILD_IMAGE` / `BASE_RUNTIME_IMAGE` | `nvidia/cuda:13.0.2-{devel,runtime}-ubuntu24.04` | 构建 / 运行阶段基础镜像 |
| `NIXL_REPO` / `NIXL_REF` | `ai-dynamo/nixl` / `v1.5.2` | NIXL 源码与版本（tag、分支或 commit） |
| `NIXL_PLUGINS` | `UCX,LIBFABRIC,GDS,GDS_MT,POSIX` | meson `enable_plugins`；列出的插件构建失败则整个构建失败。含 `LIBFABRIC` 时才编译 libfabric，含 `GDS`/`GDS_MT` 时运行镜像才装 libcufile |
| `NIXL_CUDA_ARCH` | `auto` | NIXL CUDA 代码的 SM，如 `103`、`100,103` |
| `UCX_VERSION` / `LIBFABRIC_VERSION` / `GDRCOPY_VERSION` | `v1.20.0` / `v1.21.0` / `v2.5.1` | 依赖版本 |
| `ABSL_TAG` / `GRPC_TAG` / `ETCD_CPP_REF` | `lts_2025_08_14` / `v1.73.0` / 固定 commit | NIXLBench 的 etcd 客户端依赖（Ubuntu 24.04 的 Abseil 缺 `absl/log`，须源码编译） |
| `GITHUB_URL` | `https://github.com` | 可换成 GitHub 代理 |
| `BUILD_JOBS` | `0`（CPU 数） | 并行编译数 |

新增场景：在 `image/profiles/` 加一个 `<name>.env`，CI 会自动为它多构建一个 tag。

构建说明：

- UCX 带 CUDA、verbs、rdmacm、GDRCopy 与 `--enable-mt`（NIXL progress thread 需要）。
- libfabric 启用 verbs（InfiniBand / RoCE）与 EFA（AWS）provider，CUDA、GDRCopy 用 dlopen。
- 运行镜像构建时检查：`NIXL_PLUGINS` 中每个插件都存在，`nixlbench` 与插件除驱动库（`libcuda`、`libnvidia-ml`，运行时由 NVIDIA Container Toolkit 注入）外没有缺失的动态库。
- CI 按 profile 的 `PLATFORM` 选 runner：arm64 profile 在 GitHub 托管的 `ubuntu-24.04-arm` 上原生构建（公开仓库免费），amd64 用 `ubuntu-latest`；层缓存按 profile 分开（`type=gha,scope=nixl-benchmark-<profile>`）。本地在 x86 主机上构建 arm64 profile 要走 QEMU（gRPC、UCX、NIXL 全部源码编译），需要数小时。

## 快速使用

单机 S1/S2（每台主机至少 2 张空闲 GPU）：

```bash
docker run --rm --gpus all --network host --privileged \
  -e MODE=single -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300 \
  testNixl
```

单次 nixlbench（容器内启动本地 etcd）：

```bash
docker run --rm -it --gpus all --network host --privileged \
  -e RUN_ETCD=true \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300 \
  run-nixlbench nixlbench --etcd_endpoints http://127.0.0.1:2379 \
    --backend UCX --initiator_seg_type VRAM --target_seg_type VRAM
```

libfabric 后端用 `--backend LIBFABRIC`；存储后端示例：

```bash
docker run --rm -it --gpus all --privileged -v /mnt/nvme:/data \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300 \
  nixlbench --backend POSIX --filepath /data --posix_api_type URING
```

`run-nixlbench` 变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ETCD_ENDPOINTS` | `http://127.0.0.1:2379` | etcd endpoint |
| `RUN_ETCD` | `false` | 容器内启动本地 etcd |
| `ETCD_CLEAN_PREFIX` | `RUN_ETCD=true` 时为 `true` | 运行前清理 `xferbench` prefix |
| `NIXLBENCH_DEFAULT_ARGS` | `--backend UCX --initiator_seg_type VRAM --target_seg_type VRAM` | 无参数时使用 |

## 参考

- https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench
- https://github.com/openucx/ucx
- https://github.com/ofiwg/libfabric
