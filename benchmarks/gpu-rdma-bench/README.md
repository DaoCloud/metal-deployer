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

清理 NIXLBench ETCD：

```bash
ETCDCTL_API=3 etcdctl --endpoints=http://<etcd-ip>:2379 del xferbench --prefix=true
```

## 参考

- https://github.com/deepseek-ai/DeepEP
- https://github.com/openucx/ucx
- https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench
