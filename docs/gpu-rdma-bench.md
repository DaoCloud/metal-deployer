# GPU/RDMA Bench 完整测试使用指南

## 一、概述

`gpu-rdma-bench` 是用于 GPU 服务器交付验收与互联性能验证的统一测试镜像，内置三套测试工具，均以 `test*` 脚本形式提供一键执行：

| 工具 | 脚本 | 测试内容 | 适用场景 |
|------|------|---------|---------|
| NIXLBench | `testNixl` | GPU 显存/内存传输带宽与延迟（NVLink、GPUDirect RDMA），S1-S6 验收 smoke 套件 | 单机 + 多机 |
| DeepEP | `testDeepEP` | MoE 通信算子（dispatch/combine），基于 NVSHMEM/IBGDA | 单机 + 多机 |
| UCX perftest | `testUcx` | RDMA 网卡点对点延迟/带宽 | 多机 |

镜像：

```text
ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest
```

镜像内置 `etcd`（NIXLBench rank 汇合用）、`nixlbench`、`ucx_perftest`、DeepEP/DeepGEMM Python 包与测试脚本，无需额外镜像。

**镜像预置**：`gpu-rdma-bench` 镜像随 ISO 打入系统（通过 `build-iso` 构建的自定义 Ubuntu 装机镜像预置），装机完成后被测主机即自带该镜像，无需手动导入。

## 二、准备工作

### 2.1 测试架构

测试由一台 **ansible 控制机** 统一发起：

```text
ansible 控制机 ──SSH 免密──> node1 (GPU + RDMA)
              ──SSH 免密──> node2 (GPU + RDMA)
              ──SSH 免密──> ...
```

- 所有测试命令都在 ansible 控制机上执行
- 控制机通过 SSH 在各被测主机上拉起测试容器、收集结果
- 控制机本身无需 GPU

### 2.2 文件清单（ansible 控制机上）

| 文件 | 路径 | 用途 |
|-----|------|------|
| 主机清单 | `/root/hosts` | 被测主机列表，一行一个 IP（`#` 注释行忽略） |
| SSH 私钥 | `/root/.ssh/id_rsa` | 免密登录所有被测主机 |

**hosts 文件示例：**

```text
10.20.100.215
10.20.100.240
```

验证 SSH 连通性：

```bash
for h in $(grep -vE '^\s*(#|$)' /root/hosts); do
  ssh -o BatchMode=yes root@$h hostname || echo "FAIL: $h"
done
```

### 2.3 被测主机要求

1. **GPU 与驱动**
   - NVIDIA GPU + 驱动；`testNixl` 单机测试（S1/S2）要求每节点至少 2 张空闲 GPU（显存占用 < 1GiB）
   - `testDeepEP` 对 GPU 架构与规模有硬性要求，见 4.2 节

2. **RDMA**
   - 跨节点测试需要 RDMA 网卡（InfiniBand 或 RoCE），`ibv_devices` 可见设备
   - GPUDirect RDMA 测试（S5、DeepEP low_latency）需要 `nvidia_peermem` 内核模块

3. **容器运行时**
   - Docker + NVIDIA Container Toolkit，或 containerd（nerdctl）
   - GPU-operator 部署的节点（驱动在 `/run/nvidia/driver`）上 `--gpus all` 不可用，须改用：
     `GPU_FLAGS="--runtime /usr/local/nvidia/toolkit/nvidia-container-runtime -e NVIDIA_VISIBLE_DEVICES=all"`

### 2.4 环境自检（在 ansible 控制机执行）

```bash
for h in $(grep -vE '^\s*(#|$)' /root/hosts); do
  echo "===== $h ====="
  ssh root@$h "docker run --rm --gpus all --network host --privileged \
    ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
    bash -lc 'nvidia-smi -L && ibv_devices'"
done
```

## 三、NIXLBench 测试（testNixl）

### 3.1 测试项定义

| 测试项 | 内容 | 范围 | 关注指标 |
|-------|------|------|---------|
| S1 | VRAM↔VRAM WRITE（NVLink/cuda_ipc），4K-64M | 单机 | 大块带宽 |
| S2 | VRAM↔VRAM READ + 一致性校验，4K-16M | 单机 | 带宽 + 数据正确性 |
| S3L | DRAM RDMA NIC 环回（可选，功能验证） | 单机 | 连通性 |
| S3 | DRAM↔DRAM 跨节点 RDMA WRITE，4K-64M | 多机 | 线速带宽 + 小块延迟 |
| S4 | DRAM→VRAM 跨节点（单侧 GPU），4K-1M | 多机 | 带宽 |
| S5 | VRAM↔VRAM 跨节点 + 一致性（GPUDirect RDMA），4K-1M | 多机 | 带宽 + 数据正确性 |
| S6 | DRAM 大块带宽压测，64K-64M，iter 200 | 多机 | 持续带宽 |

设计原则：小块看延迟、大块看带宽；READ/WRITE 各覆盖一个方向；一致性校验至少一轮（抓带宽正常但数据错误的链路/内存故障）。

### 3.2 模式说明

| 模式 | 测试内容 | 用途 |
|------|---------|------|
| `MODE=all`（**推荐**） | phase 1：每台 host **并行**跑单机 S1/S2（各容器自起本地 etcd，互不干扰）；phase 2：按 pairwise 拓扑**串行**跑跨节点 S3-S6 | 验收标准流程，一条命令全覆盖 |
| `MODE=single` | 仅当前节点 S1/S2 | 定位单机 GPU/驱动问题，或快速复测某一台 |
| `MODE=multi` | 仅跨节点 S3-S6 | 单机已确认正常，只复测网络 |

pairwise 拓扑：hosts 按顺序 `(h1,h2)(h3,h4)...`；奇数台时 h1 补测最后一台。例：7 台 → `(h1,h2)(h3,h4)(h5,h6)(h1,h7)`。

etcd（跨节点 rank 汇合用，测试期间只需一个实例）：`START_ETCD=true` 时 testNixl 在当前（控制机）容器内启动镜像自带的 etcd 二进制，对外通告地址自动探测（`ip route get <第一台 host>` 的 src 地址），各被测节点主动连接，**无需任何 IP 配置**；也可用 `ETCD_ENDPOINTS` 指向已有 etcd（见 3.4）。

### 3.3 标准验收测试（在 ansible 控制机执行）

```bash
docker run --rm --network host \
  -v /root/hosts:/hosts:ro \
  -v /root/.ssh:/root/.ssh:ro \
  -e MODE=all -e HOSTS_FILE=/hosts \
  -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl
```

即完成：etcd 在控制机容器内启动（通告地址自动探测）→ 每台单机 S1/S2（并行）→ 两两跨节点 S3-S6（串行）→ 输出汇总报告。

只测单机（登录某台被测机执行，或从控制机 SSH 过去）：

```bash
ssh root@10.20.100.215 "docker run --rm --gpus all --network host --privileged \
  -e MODE=single -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl"
```

只测跨节点（控制机执行）：

```bash
docker run --rm --network host \
  -v /root/hosts:/hosts:ro \
  -v /root/.ssh:/root/.ssh:ro \
  -e MODE=multi -e HOSTS_FILE=/hosts \
  -e START_ETCD=true \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl
```

先演练（只打印将执行的命令，不实际运行）：在上述命令中加 `-e DRY_RUN=true`。

### 3.4 使用控制机上的 etcd（可选）

如果希望 etcd 固定跑在控制机上（例如反复多轮测试），先启动：

```bash
docker run -d --name nixl-etcd --network host \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  etcd --data-dir=/tmp/etcd \
    --listen-client-urls=http://0.0.0.0:12379 \
    --advertise-client-urls=http://0.0.0.0:12379
```

之后测试命令用 `ETCD_ENDPOINTS` 替换 `START_ETCD`（`ETCD_IP` 自动取控制机地址）：

```bash
ETCD_IP=$(hostname -I | awk '{print $1}')
docker run --rm --network host \
  -v /root/hosts:/hosts:ro \
  -v /root/.ssh:/root/.ssh:ro \
  -e MODE=all -e HOSTS_FILE=/hosts \
  -e ETCD_ENDPOINTS=http://${ETCD_IP}:12379 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testNixl
```

### 3.5 常用变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MODE` | `single` | `single` / `multi` / `all` |
| `HOSTS_FILE` | - | multi/all 必须，≥2 台 |
| `TOPOLOGY` | `pairwise` | 分组拓扑 |
| `TESTS` | 按模式 | 覆盖测试项，如 `"s1 s2"` / `"s3 s6"` |
| `START_ETCD` | `false` | 控制机容器内启动 etcd，通告地址自动探测 |
| `ETCD_ADVERTISE_IP` | 自动探测 | etcd 对外通告地址，多网卡等场景可显式指定 |
| `ETCD_ENDPOINTS` | - | 使用已有 etcd（与 START_ETCD 二选一） |
| `IMAGE` | 本镜像 | 被测 host 上启动的镜像 |
| `CONTAINER_RUNTIME` | `docker` | `docker` / `nerdctl` |
| `GPU_FLAGS` | `--gpus all` | GPU-operator 节点见 2.3 节 |
| `DEVICE_LIST` | `all` | RDMA 网卡；`all`=全部设备（UCX 多轨聚合），可指定如 `mlx5_0` 隔离单网卡 |
| `GPUS` | 自动挑 2 张空闲卡 | 单机测试用卡，如 `"2,3"` |
| `NUM_ITER` / `WARMUP_ITER` | `100` / `10` | 迭代数 |
| `TEST_TIMEOUT` | `900` | 单项超时秒数 |
| `WORKDIR` | `/tmp/testNixl-<ts>` | 日志与报告目录 |
| `DRY_RUN` | `false` | 演练模式 |

### 3.6 结果解读

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
10.20.100.240/s1         PASS   ...
10.20.100.240/s2         PASS   ...
10.20.100.215->10.20.100.240/s3 PASS   4096B lat=4.2us | 67108864B bw=24.677561GB/s
10.20.100.215->10.20.100.240/s4 PASS   4096B lat=4.7us | 1048576B bw=17.724194GB/s
10.20.100.215->10.20.100.240/s5 PASS   4096B lat=5.3us | 1048576B bw=11.887895GB/s
10.20.100.215->10.20.100.240/s6 PASS   65536B lat=7.1us | 67108864B bw=24.548109GB/s
----------------------------------------------------------
summary: 8/8 PASS
==========================================================
```

DETAIL 格式：首行（最小块）延迟 + 末行（最大块）带宽。判定逻辑：initiator 退出码为 0、日志有结果表、无 error/mismatch 关键字则 PASS。

**实测参考基线：**

| 环境 | 测试 | 结果 |
|------|------|------|
| H100 单机（NVLink） | S1 | 295-341 GB/s |
| H100 单机 | S2 | 177-178 GB/s |
| A800 跨节点 200Gb IB | S3 | 24.5-24.7 GB/s（≈线速） |
| A800 跨节点 | S4 | 17.7 GB/s |
| A800 跨节点（GPUDirect） | S5 | 11.9-13.0 GB/s |
| A800 跨节点 | S6 | 24.5-24.8 GB/s |
| A800 跨节点 | 4KB 延迟 | 4.2-6.2 µs |

验收判据建议：S3/S6 达到网卡线速 90% 以上；S5 一致性校验无 mismatch；4KB 延迟 < 10µs（同机房 IB/RoCE）。

## 四、DeepEP 测试（testDeepEP）

### 4.1 测试项

- `test_intranode.py`：单节点多 GPU dispatch/combine（NVLink）—— 默认开启
- `test_low_latency.py`：低延迟模式（IBGDA/GPUDirect）—— 默认开启
- `test_internode.py`：跨节点 dispatch/combine（NVSHMEM/RDMA）—— 默认关闭，`RUN_INTERNODE=true` 开启

### 4.2 架构与规模约束

不满足时测试无法运行，属预期而非环境故障：

| 约束 | 说明 |
| --- | --- |
| GPU 架构 | 镜像 DeepEP 按 SM90（H100/H800）编译；其他架构（如 A100/A800）会报 `CUDA error 'named symbol not found'`，无法运行 |
| intranode/low_latency | 每节点至少 2 张 GPU |
| internode 常规模式 | 总 ranks（节点数 × 每节点卡数）必须 > 8，如 2 节点 × 8 卡 |
| internode low_latency | 需要 `nvidia_peermem` 已加载 |

### 4.3 执行（在 ansible 控制机执行）

单节点测试（默认模式，SSH 到目标机运行；对每台机器循环即可全覆盖）：

```bash
for h in $(grep -vE '^\s*(#|$)' /root/hosts); do
  echo "===== $h ====="
  ssh root@$h "docker run --rm --gpus all --network host --privileged \
    -v /dev/infiniband:/dev/infiniband \
    ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
    testDeepEP"
done
```

跨节点测试（testDeepEP 的 driver 必须运行在 hosts 列表中的某一台上——用第一台；它会 SSH 编排其余节点）：

```bash
FIRST_HOST=$(grep -vE '^\s*(#|$)' /root/hosts | head -1)
scp /root/hosts root@${FIRST_HOST}:/root/hosts
ssh root@${FIRST_HOST} "docker run --rm --gpus all --network host --privileged \
  -v /dev/infiniband:/dev/infiniband \
  -v /root/hosts:/hosts:ro -v /root/.ssh:/root/.ssh:ro \
  -e RUN_INTERNODE=true -e RUN_INTRANODE=false -e RUN_LOW_LATENCY=false \
  -e HOSTS_FILE=/hosts -e SSH_USER=root -e SSH_PORT=22 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testDeepEP"
```

注意：跨节点测试要求第一台能免密 SSH 到 hosts 中所有其他节点。

### 4.4 常用变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUN_INTRANODE` | `true` | 单节点 dispatch/combine |
| `RUN_LOW_LATENCY` | `true` | 低延迟模式 |
| `RUN_INTERNODE` | `false` | 跨节点测试 |
| `HOSTS_FILE` | - | internode 必须；driver 须在列表内 |
| `INTERNODE_NUM_PROCESSES` | 自动检测 | 每节点 GPU/进程数 |
| `NCCL_IB_DISABLE` | - | 个别节点 NCCL IB 初始化崩溃时设 `1` 绕过（NCCL 仅控制面，数据面走 NVSHMEM，不影响结果） |
| `REPORT_FILE` | `/tmp/deepep_benchmark_results.json` | JSON 报告 |
| `DEBUG` | `false` | 输出 internode 诊断信息 |

报告写入 `REPORT_FILE`（JSON），包含各测试项 status/duration/rc 及 internode 各 host 日志路径。

## 五、UCX RDMA 测试（testUcx，在 ansible 控制机执行）

点对点 RDMA 延迟/带宽（详细变量见 `benchmarks/gpu-rdma-bench/README.md`）：

```bash
docker run --rm --privileged --network host \
  -v /root/hosts:/hosts:ro \
  -v /root/.ssh:/root/.ssh:ro \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=infiniband \
  -e SAME_RAIL_ONLY=true \
  -e NUM_ITERS=10000 \
  ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:latest \
  testUcx
```

报告：`/tmp/ucx_rdma_benchmark_report.json`。

## 六、推荐验收流程

全部在 ansible 控制机上依次执行：

1. **环境自检**：2.4 节循环，确认每台 GPU/RDMA 设备可见
2. **UCX 网卡点对点**：第五章 `testUcx`，确认 RDMA 链路延迟/带宽正常
3. **NIXLBench 全量**：3.3 节 `MODE=all testNixl`，一条命令覆盖每台单机 + 两两跨节点，与 3.6 基线比对
4. **DeepEP**（架构与规模满足时）：4.3 节单节点循环；≥2 节点 × 8 卡时再跑跨节点
5. 收集 `${WORKDIR}/report.txt`、`*.log` 与 JSON 报告存档

## 七、常见问题

**Q: `Need 2 free GPUs` 失败？**
A: S1/S2 需要每节点 2 张空闲 GPU（显存占用 < 1GiB）。GPU 被占用时属预期失败；也可 `GPUS=i,j` 强制指定。

**Q: GPU-operator 节点容器起不来（`nvidia-container-cli not found` / `libnvidia-ml.so missing`）？**
A: 用 `GPU_FLAGS="--runtime /usr/local/nvidia/toolkit/nvidia-container-runtime -e NVIDIA_VISIBLE_DEVICES=all"`；宿主机上执行 `chroot /run/nvidia/driver nvidia-smi` 验证驱动。

**Q: 跨节点测试卡住不动？**
A: 依次排查：etcd 是否可达（`etcdctl --endpoints=... endpoint health`）；两端容器是否都已启动（后启动方需在 60 秒内加入）；RDMA 设备名 `DEVICE_LIST` 是否正确。

**Q: DeepEP 报 `CUDA error 'named symbol not found'`？**
A: GPU 架构与镜像编译目标（SM90/Hopper）不符，如 A800/SM80 节点无法运行 DeepEP 测试。

**Q: DeepEP internode 报 `Assertion 'num_ranks > NUM_MAX_NVL_PEERS'`？**
A: 常规 internode 要求总 ranks > 8，设计约束。加大节点/卡数，或只测 low_latency。

**Q: 个别节点 NCCL 初始化 SIGSEGV？**
A: 设 `NCCL_IB_DISABLE=1` 绕过（节点 NCCL/IB 环境问题；DeepEP 中 NCCL 仅做控制面）。

## 八、参考文档

- [benchmarks/gpu-rdma-bench/README.md](../benchmarks/gpu-rdma-bench/README.md) — 镜像使用速查
- [nixlbench-acceptance-smoke-suite.md](../benchmarks/gpu-rdma-bench/nixlbench-acceptance-smoke-suite.md) — S1-S6 定义与判定标准
- [DeepEP](https://github.com/deepseek-ai/DeepEP)
- [NIXL / nixlbench](https://github.com/ai-dynamo/nixl/tree/main/benchmark/nixlbench)
- [UCX](https://github.com/openucx/ucx)
