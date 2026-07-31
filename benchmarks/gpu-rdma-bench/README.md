# GPU/RDMA Bench 一键测试指南

本目录提供统一 benchmark 镜像，包含：

- DeepEP: GPU 通信 benchmark
- UCX perftest: RDMA 延迟/带宽 benchmark
- NIXLBench: NIXL UCX/libfabric benchmark

镜像：

```text
ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest
```

## 前置条件

| 项 | 要求 |
| --- | --- |
| GPU | NVIDIA GPU + 驱动 |
| 容器运行时 | Docker + NVIDIA Container Toolkit |
| RDMA | UCX/NIXL 网络测试需要 RDMA 设备或可用 Ethernet |
| SSH | DeepEP internode 和 UCX 多主机测试需要节点间免密 SSH |
| ETCD | NIXLBench 网络后端多实例测试需要 ETCD |

## 构建镜像

```bash
make build-gpu-rdma-bench
```

## DeepEP 测试

架构与规模要求（不满足时测试无法运行，属预期而非环境故障）：

| 约束 | 说明 |
| --- | --- |
| GPU 架构 | 默认镜像按 `TORCH_CUDA_ARCH_LIST=9.0`（Hopper/SM90，H100/H800）编译。A100/A800（SM80）需重建镜像：`--build-arg ENV_TORCH_CUDA_ARCH_LIST="8.0"`（构建脚本会自动启用 `DISABLE_SM90_FEATURES=1`，SM80 与 SM90 不能混编到同一镜像） |
| 单节点（intranode/low_latency） | 每节点至少 2 张 GPU |
| 多节点 internode 常规模式 | 总 ranks（节点数 × 每节点 GPU 数）必须大于 8，例如 2 节点 × 8 卡 |
| 多节点 low_latency | 需要 GPUDirect RDMA（`nvidia_peermem` 内核模块已加载） |

单节点：

```bash
docker run --rm --gpus all \
  -e RUN_INTERNODE=false \
  -e RUN_INTRANODE=true \
  -e RUN_LOW_LATENCY=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testDeepEP
```

多节点：

```bash
docker run --rm --gpus all \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RUN_INTERNODE=true \
  -e RUN_INTRANODE=true \
  -e RUN_LOW_LATENCY=true \
  -e SSH_USER=root \
  -e SSH_PORT=22 \
  -e INTERNODE_NUM_PROCESSES=8 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testDeepEP
```

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_INTRANODE` | `true` | 运行单节点测试 |
| `RUN_LOW_LATENCY` | `true` | 运行低延迟测试 |
| `RUN_INTERNODE` | `false` | 运行多节点测试 |
| `HOSTS_FILE` | - | 节点列表 |
| `SSH_USER` | `root` | SSH 用户 |
| `SSH_PORT` | `2022` | SSH 端口 |
| `INTERNODE_NUM_PROCESSES` | 自动检测 | 每节点 GPU/进程数 |
| `REPORT_FILE` | `/tmp/deepep_benchmark_results.json` | 报告路径 |

## UCX RDMA 测试

InfiniBand：

```bash
docker run --rm --privileged \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=infiniband \
  -e SAME_RAIL_ONLY=true \
  -e NUM_ITERS=10000 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testUcx
```

Ethernet：

```bash
docker run --rm --privileged \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=ethernet \
  -e SAME_NETWORK_TRACK=true \
  -e NUM_ITERS=5000 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testUcx
```

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOSTS_FILE` | - | 节点列表，必须 |
| `SSH_USER` | `root` | SSH 用户 |
| `SSH_PORT` | `2022` | SSH 端口 |
| `RDMA_MODE` | `infiniband` | `infiniband` 或 `ethernet` |
| `SAME_NETWORK_TRACK` | `true` | 客户端/服务端同子网 |
| `SAME_RAIL_ONLY` | `true` | 只测同 rail |
| `TESTS` | `tag_lat tag_bw` | UCX 测试项 |
| `NUM_ITERS` | `10000` | 迭代数 |
| `REPORT_FILE` | `/tmp/ucx_rdma_benchmark_report.json` | 报告路径 |

## NIXLBench 测试

### 自动化：testNixl（推荐）

`testNixl` 是镜像内置的验收 smoke 测试驱动，自动编排 S1-S6 测试项并输出 PASS/FAIL 报告（测试项定义与判定标准见 [nixlbench-acceptance-smoke-suite.md](./nixlbench-acceptance-smoke-suite.md)）：

- S1/S2：单机 VRAM↔VRAM WRITE/READ（NVLink/cuda_ipc）+ 一致性校验
- S3-S6：跨节点 DRAM/VRAM RDMA 带宽、延迟、GPUDirect、一致性、大块压测

单机模式（在被测节点直接运行，需 ≥2 张空闲 GPU）：

```bash
docker run --rm --gpus all --network host --privileged \
  -e MODE=single -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl
```

多机模式（driver 节点运行，通过 SSH 在各 host 上起容器；hosts 文件一行一个地址）：

```bash
docker run --rm --network host --privileged \
  -v /path/to/hosts:/hosts -v /root/.ssh:/root/.ssh:ro \
  -e MODE=multi -e HOSTS_FILE=/hosts \
  -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl
```

`MODE=all` 先并行跑每台 host 的单机测试（各容器自起本地 etcd，互不干扰），再串行跑跨节点 pairwise 测试。

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MODE` | `single` | `single` / `multi` / `all` |
| `HOSTS_FILE` | - | 节点列表（multi/all 必须，≥2 台） |
| `TOPOLOGY` | `pairwise` | 两两分组；奇数台时 h1 补测最后一台 |
| `TESTS` | 按模式 | 覆盖测试项，如 `"s1 s2"` / `"s3 s6"` |
| `START_ETCD` | `false` | driver 容器内启动 etcd（需 hostNetwork）；multi/all 通告地址自动探测 |
| `ETCD_ADVERTISE_IP` | 自动探测（`ip route get` 第一台 host 的 src） | 多网卡等场景可显式指定 |
| `ETCD_ENDPOINTS` | - | 使用外部 etcd |
| `IMAGE` | 本镜像 | 远端 host 上启动的镜像（须已导入，不自动拉取） |
| `CONTAINER_RUNTIME` | `docker` | `docker` / `nerdctl` |
| `GPU_FLAGS` | `--gpus all` | GPU-operator 节点用 `--runtime /usr/local/nvidia/toolkit/nvidia-container-runtime -e NVIDIA_VISIBLE_DEVICES=all` |
| `DEVICE_LIST` | `all` | RDMA 网卡；`all`=全部设备（UCX 多轨聚合），可指定如 `mlx5_0` 隔离单网卡 |
| `DRY_RUN` | `false` | 只打印将执行的命令 |
| `TESTNIXL_MOUNT` | - | 迭代调试：挂载宿主机脚本覆盖镜像内版本，如 `-v /tmp/testNixl:/usr/sbin/testNixl:ro` |

完整变量与测试逻辑见脚本头部注释（`image/tools/testNixl`）。

### 手动：run-nixlbench

单机快速测试。容器内启动本地 ETCD，并运行 UCX VRAM 默认测试：

```bash
docker run --rm -it --gpus all --network host --privileged \
  -e RUN_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  run-nixlbench
```

传入 nixlbench 参数：

```bash
docker run --rm -it --gpus all --network host --privileged \
  -e RUN_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  run-nixlbench nixlbench --etcd_endpoints http://127.0.0.1:2379 \
    --backend UCX \
    --initiator_seg_type VRAM \
    --target_seg_type VRAM \
    --num_iter 1000 \
    --warmup_iter 100
```

多节点 UCX/NIXL 测试，先启动 ETCD：

```bash
docker run -d --name nixlbench-etcd --network host \
  quay.io/coreos/etcd:v3.5.18 \
  /usr/local/bin/etcd \
    --data-dir=/etcd-data \
    --listen-client-urls=http://0.0.0.0:2379 \
    --advertise-client-urls=http://<etcd-ip>:2379 \
    --listen-peer-urls=http://0.0.0.0:2380 \
    --initial-advertise-peer-urls=http://0.0.0.0:2380 \
    --initial-cluster=default=http://0.0.0.0:2380
```

每个测试节点启动一个实例，第二个及后续实例需在 60 秒内启动：

```bash
docker run --rm -it --gpus all --network host --privileged \
  -e ETCD_ENDPOINTS=http://<etcd-ip>:2379 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  run-nixlbench nixlbench --etcd_endpoints http://<etcd-ip>:2379 \
    --backend UCX \
    --initiator_seg_type VRAM \
    --target_seg_type VRAM \
    --device_list mlx5_0,mlx5_1
```

Libfabric backend 示例：

```bash
docker run --rm -it --gpus all --network host --privileged \
  -e ETCD_ENDPOINTS=http://<etcd-ip>:2379 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  run-nixlbench nixlbench --etcd_endpoints http://<etcd-ip>:2379 \
    --backend Libfabric \
    --initiator_seg_type VRAM \
    --target_seg_type VRAM
```

NIXLBench 常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ETCD_ENDPOINTS` | `http://127.0.0.1:2379` | ETCD endpoint |
| `RUN_ETCD` | `false` | 容器内启动本地 ETCD |
| `ETCD_CLEAN_PREFIX` | `RUN_ETCD=true` 时为 `true`，否则为 `false` | 运行前清理 `xferbench` prefix |
| `NIXLBENCH_DEFAULT_ARGS` | `--backend UCX --initiator_seg_type VRAM --target_seg_type VRAM` | 无参数时使用 |

## 排障

检查 GPU/RDMA/UCX：

```bash
docker run --rm -it --gpus all --network host --privileged \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  bash -lc 'nvidia-smi && ibv_devices && ucx_info -d'
```

DeepEP 常见问题：

| 现象 | 原因与处理 |
| --- | --- |
| `CUDA error 'named symbol not found'` | GPU 架构与镜像编译目标不符（如 SM80 GPU 跑 SM90 镜像），按上文重建镜像 |
| `Assertion 'num_ranks > NUM_MAX_NVL_PEERS'` | internode 常规模式要求总 ranks > 8，加大节点/GPU 数或只测 low_latency |
| NCCL 初始化 SIGSEGV（个别节点） | 可设 `NCCL_IB_DISABLE=1` 绕过（DeepEP 中 NCCL 仅做控制面，数据面走 NVSHMEM，不影响结果） |
| SSH 远端 `No module named deep_ep` | 旧镜像缺 site-packages `.pth` 注册，重建镜像即可 |

清理 NIXLBench ETCD：

```bash
ETCDCTL_API=3 etcdctl --endpoints=http://<etcd-ip>:2379 del xferbench --prefix=true
```

## 参考

- https://github.com/deepseek-ai/DeepEP
- https://github.com/openucx/ucx
- https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench
