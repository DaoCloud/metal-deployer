# NIXL Benchmark 测试使用指南

## 一、概述

`nixl-benchmark` 是 NIXL / NIXLBench 测试镜像，用来验证 GPU 显存与内存之间的传输带宽和延迟：节点内 NVLink、跨节点 RDMA 与 GPUDirect RDMA，以及经 NIXL 的本机存储（POSIX、GDS）。验收用 S1-S6 smoke 套件，由镜像内置脚本 `testNixl` 一键执行，也可以由 testkit 调度（见第五章）。

| 工具 | 内容 |
| --- | --- |
| `testNixl` | S1-S6 验收 smoke 套件驱动：单机 + 两两跨节点，输出 PASS/FAIL 报告 |
| `run-nixlbench` | 单次运行 nixlbench，可在容器内启动本地 etcd |
| `nixlbench`、`etcd` | NIXLBench 本体与进程汇合用的 etcd |

镜像（每个 profile 一个 tag，构建与 profile 见 [benchmarks/nixl-benchmark/README.md](../benchmarks/nixl-benchmark/README.md)）：

```text
ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300   # GB300, arm64
ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-x86     # B200/B300 x86
```

镜像不随 ISO 预置，被测主机需提前导入（`docker pull` 或 `docker load`）；`testNixl` 不会自动拉取。

## 二、准备工作

### 2.1 测试架构

测试由一台控制机统一发起：

```text
控制机 ──SSH 免密──> node1 (GPU + RDMA)
       ──SSH 免密──> node2 (GPU + RDMA)
       ──SSH 免密──> ...
```

- 所有命令在控制机上执行，控制机通过 SSH 在各被测主机上起测试容器、收集结果，本身无需 GPU。
- 控制机上运行的 driver 容器须与控制机同架构：x86 控制机测 GB300 时，driver 用 `latest-cuda13-x86`，被测主机上的镜像用 `IMAGE=...:latest-cuda13-gb300` 指定。

### 2.2 控制机上的文件

| 文件 | 路径 | 用途 |
|-----|------|------|
| 主机清单 | `/root/hosts` | 被测主机，一行一个 IP（`#` 注释行忽略） |
| SSH 私钥 | `/root/.ssh/id_rsa` | 免密登录所有被测主机 |

### 2.3 被测主机要求

1. NVIDIA GPU 与驱动；单机测试（S1/S2）要求每台至少 2 张空闲 GPU（显存占用 < 1GiB）。
2. 跨节点测试需要 RDMA 网卡（InfiniBand 或 RoCE），`ibv_devices` 可见；GPUDirect RDMA（S5）需要 `nvidia_peermem` 内核模块。
3. Docker + NVIDIA Container Toolkit，或 containerd（nerdctl）。GPU-operator 部署的节点上 `--gpus all` 不可用，须改用 `GPU_FLAGS="--runtime /usr/local/nvidia/toolkit/nvidia-container-runtime -e NVIDIA_VISIBLE_DEVICES=all"`。

## 三、testNixl 测试

### 3.1 测试项

| 测试项 | 内容 | 范围 | 关注指标 |
|-------|------|------|---------|
| S1 | VRAM↔VRAM WRITE（NVLink/cuda_ipc），4K-64M | 单机 | 大块带宽 |
| S2 | VRAM↔VRAM READ + 一致性校验，4K-16M | 单机 | 带宽 + 数据正确性 |
| S3L | DRAM RDMA 网卡环回（可选，功能验证） | 单机 | 连通性 |
| S3 | DRAM↔DRAM 跨节点 RDMA WRITE，4K-64M | 多机 | 线速带宽 + 小块延迟 |
| S4 | DRAM→VRAM 跨节点（单侧 GPU），4K-1M | 多机 | 带宽 |
| S5 | VRAM↔VRAM 跨节点 + 一致性（GPUDirect RDMA），4K-1M | 多机 | 带宽 + 数据正确性 |
| S6 | DRAM 大块带宽压测，64K-64M，iter 200 | 多机 | 持续带宽 |

定义与判定标准见 [nixlbench-acceptance-smoke-suite.md](../benchmarks/nixl-benchmark/nixlbench-acceptance-smoke-suite.md)。

### 3.2 模式

| 模式 | 测试内容 | 用途 |
|------|---------|------|
| `MODE=all`（推荐） | 第一阶段每台主机并行跑单机 S1/S2（各容器自起本地 etcd）；第二阶段按 pairwise 拓扑串行跑跨节点 S3-S6 | 验收标准流程 |
| `MODE=single` | 当前节点 S1/S2 | 定位单机 GPU / 驱动问题 |
| `MODE=multi` | 跨节点 S3-S6 | 单机已正常，只复测网络 |

pairwise 拓扑：`(h1,h2)(h3,h4)...`，奇数台时 h1 再与最后一台配对。例：7 台 → `(h1,h2)(h3,h4)(h5,h6)(h1,h7)`。

etcd：`START_ETCD=true` 时 testNixl 在控制机容器内启动镜像自带的 etcd，对外通告地址自动探测（`ip route get <第一台 host>` 的 src 地址）；也可用 `ETCD_ENDPOINTS` 指向已有 etcd。

### 3.3 标准验收（控制机执行）

```bash
docker run --rm --network host \
  -v /root/hosts:/hosts:ro \
  -v /root/.ssh:/root/.ssh:ro \
  -e MODE=all -e HOSTS_FILE=/hosts \
  -e START_ETCD=true \
  -e IMAGE=ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300 \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-x86 \
  testNixl
```

控制机与被测主机同架构时，driver 与 `IMAGE` 用同一个 tag。只测单机（登录被测主机执行）：

```bash
docker run --rm --gpus all --network host --privileged \
  -e MODE=single -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-gb300 \
  testNixl
```

只测跨节点：把 3.3 命令中的 `MODE=all` 改为 `MODE=multi`。先演练（只打印命令）：加 `-e DRY_RUN=true`。

### 3.4 使用固定的 etcd（可选）

反复多轮测试时可让 etcd 常驻控制机：

```bash
docker run -d --name nixl-etcd --network host \
  ghcr.io/daocloud/metal-deployer/nixl-benchmark:latest-cuda13-x86 \
  etcd --data-dir=/tmp/etcd \
    --listen-client-urls=http://0.0.0.0:12379 \
    --advertise-client-urls=http://0.0.0.0:12379
```

之后用 `-e ETCD_ENDPOINTS=http://<控制机 IP>:12379` 替换 `START_ETCD`。

### 3.5 常用变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MODE` | `single` | `single` / `multi` / `all` |
| `HOSTS_FILE` | - | multi/all 必须，≥2 台 |
| `TOPOLOGY` | `pairwise` | 分组拓扑 |
| `TESTS` | 按模式 | 覆盖测试项，如 `"s1 s2"` / `"s3 s6"` |
| `START_ETCD` | `false` | 控制机容器内启动 etcd，通告地址自动探测 |
| `ETCD_ADVERTISE_IP` | 自动探测 | 多网卡等场景可显式指定 |
| `ETCD_ENDPOINTS` | - | 使用已有 etcd（与 `START_ETCD` 二选一） |
| `IMAGE` | `...nixl-benchmark:latest-cuda13-gb300` | 被测主机上启动的镜像（须已导入） |
| `CONTAINER_RUNTIME` | `docker` | `docker` / `nerdctl` |
| `GPU_FLAGS` | `--gpus all` | GPU-operator 节点见 2.3 |
| `DEVICE_LIST` | `all` | RDMA 网卡；`all` 为全部设备（UCX 多轨聚合），可指定如 `mlx5_0` 隔离单卡 |
| `GPUS` | 自动挑 2 张空闲卡 | 单机测试用卡，如 `"2,3"` |
| `REMOTE_GPU` | `0` | 跨节点 VRAM 测试在远端用的 GPU |
| `NUM_ITER` / `WARMUP_ITER` | `100` / `10` | 迭代数 |
| `TEST_TIMEOUT` | `900` | 单项超时秒数 |
| `WORKDIR` | `/tmp/testNixl-<ts>` | 日志与报告目录 |
| `DRY_RUN` | `false` | 演练模式 |
| `TESTNIXL_MOUNT` | - | 调试：挂载宿主机脚本覆盖镜像内版本，如 `-v /tmp/testNixl:/usr/local/bin/testNixl:ro` |

完整变量与测试逻辑见脚本头部注释（`benchmarks/nixl-benchmark/image/tools/testNixl`）。

## 四、结果解读

运行结束输出报告（同时写入 `${WORKDIR}/report.txt`），全部 PASS 时退出码为 0：

```text
==================== testNixl report ====================
mode=all topology=pairwise etcd=http://10.20.100.215:12379 device=mlx5_0
hosts(2): 10.20.100.215 10.20.100.240
pairs(1): 10.20.100.215->10.20.100.240
logs: /tmp/testNixl-20260729
----------------------------------------------------------
TEST                     STATUS DETAIL
10.20.100.215/s1         PASS   4096B lat=12.1us | 67108864B bw=341.2GB/s
10.20.100.215/s2         PASS   4096B lat=13.0us | 16777216B bw=178.4GB/s
10.20.100.215->10.20.100.240/s3 PASS   4096B lat=4.2us | 67108864B bw=24.677561GB/s
10.20.100.215->10.20.100.240/s5 PASS   4096B lat=5.3us | 1048576B bw=11.887895GB/s
----------------------------------------------------------
summary: 8/8 PASS
==========================================================
```

DETAIL 为首行（最小块）延迟 + 末行（最大块）带宽。initiator 退出码为 0、有结果表、日志中没有 error/mismatch 关键字时 PASS。

实测参考（迁移前的旧镜像，CUDA 12.8，NIXL main）：

| 环境 | 测试 | 结果 |
|------|------|------|
| H100 单机（NVLink） | S1 | 295-341 GB/s |
| H100 单机 | S2 | 177-178 GB/s |
| A800 跨节点 200Gb IB | S3 | 24.5-24.7 GB/s（≈线速） |
| A800 跨节点 | S4 | 17.7 GB/s |
| A800 跨节点（GPUDirect） | S5 | 11.9-13.0 GB/s |
| A800 跨节点 | S6 | 24.5-24.8 GB/s |
| A800 跨节点 | 4KB 延迟 | 4.2-6.2 µs |

GB300 / B300 的基线待金样本实测后补充。验收判据见 smoke 套件文档。

## 五、在 testkit 中运行

GB300 验收由 testkit 调度时，在 `tests.yaml`（`testkit init` 生成的模板已有 `nixl` 段）设置 `images.nixl_benchmark`，把 `nixl.node`、`nixl.pair`（可选 `nixl.storage`）改为 `enabled: true`：

```yaml
images:
  nixl_benchmark: registry.local/metal-deployer/nixl-benchmark:latest-cuda13-gb300
nixl:
  node:
    enabled: true
    scope: node
    hosts: all
    cases: [s1, s2]
  pair:
    enabled: true
    scope: rack
    hosts: all
    cases: [s3, s4, s5, s6]
    pairs: rack_adjacent          # 柜内 tray 1-2、3-4 ...
```

```bash
testkit run nixl
```

testkit 不使用 testNixl 的 SSH 编排，而是用自己的 mpi 模式容器（rank 0 容器启动 etcd，其他主机的进程经容器内 sshd 启动），结果进入 testkit 报告。细节见 [benchmarks/testkit/README.md](../benchmarks/testkit/README.md#nixlnixlbench可选默认关闭)。

## 六、常见问题

**Q: `Need 2 free GPUs` 失败？**
A: S1/S2 需要每台 2 张空闲 GPU（显存占用 < 1GiB）。GPU 被占用时属预期失败；可用 `GPUS=i,j` 指定。

**Q: 跨节点测试卡住？**
A: 依次检查：etcd 是否可达（`etcdctl --endpoints=... endpoint health`）；两端容器是否都已启动（后启动方须在 60 秒内加入）；`DEVICE_LIST` 中的网卡名是否正确。

**Q: 清理残留的 etcd key？**
A: `ETCDCTL_API=3 etcdctl --endpoints=http://<etcd-ip>:2379 del xferbench --prefix=true`

**Q: GPU-operator 节点容器起不来？**
A: 用 2.3 中的 `GPU_FLAGS`；宿主机上 `chroot /run/nvidia/driver nvidia-smi` 验证驱动。

## 七、参考

- [benchmarks/nixl-benchmark/README.md](../benchmarks/nixl-benchmark/README.md)：镜像、profile 与构建
- [nixlbench-acceptance-smoke-suite.md](../benchmarks/nixl-benchmark/nixlbench-acceptance-smoke-suite.md)：S1-S6 定义与判定标准
- [NIXL / nixlbench](https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench)
