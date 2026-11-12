# SuperBench 适配 GB300

日期：2026-11-06 | 状态：配置已实施并通过离线校验，全部条目尚未在 GB300 实机运行。

本文记录 SuperBench（sbcli 控制端 + worker 被测镜像）为适配 GB300 / GB300 NVL72 所做的改动、依据和实机待确认项。验收口径参考飞书文档《GB 300的测试验收》。

## 1. 结论与范围

- **worker 镜像不重建**：继续使用 GB300 ISO 预置的 `superbench/main:cuda13.0`（main@9142bdfe，含 sm_103，CUDA 13.0 与 ISO 的 580 驱动匹配）。主要缺口在配置，不在镜像。
- **改动集中在本仓库**：新增 `config_gb300_*.yaml` 和 `rule_gb300.yaml`，`run-tests.sh` 增加 gb300 profile。x86 的原有配置和默认行为不变。
- **不放进 SuperBench 的测试**（见第 7 节）：Hybrid-EP / DeepGEMM、IMEX 数据面测试由 moe-bench 镜像负责；多节点 nvbandwidth、NVLoom 由宿主机 DCGM `dcgmi mndiag` 负责；IMEX 状态检查、stress-ng、Grace HPL 留在宿主机。

## 2. GB300 硬件假设（实机确认前的暂定值）

| 项 | x86 配置假设 | GB300 每 tray | 配置中的处理 |
|-|-|-|-|
| GPU 数 | 8 | 4（B300） | 所有 `proc_num` 为 4 |
| CPU | x86（Intel/AMD） | 2 × Grace（Neoverse V2，arm64），每颗 72 核 | cpu-stream 用 `neo2`；节点 0 = 核 0–71，节点 1 = 核 72–143 |
| 计算网卡 | 8 × IB | 4 × ConnectX-8，每卡单口 800G XDR；rail 对齐（GPU N → Rail N），IB 单平面 | 按 IB link-layer 设备排序后第 N 个对应 GPU N |
| 其他网卡 | — | BlueField-3（南北向）也会显示为 `mlx5_*` | SuperBench 只选 link_layer 为 InfiniBand 的设备，BF3 为 Ethernet 时自动排除 |
| GPU/NIC 亲和 | — | GPU 0/1、NIC 0/1 在 Grace 0；GPU 2/3、NIC 2/3 在 Grace 1 | `numa_dev: $((LOCAL_RANK/2))`；ib-loopback 每组一 NIC 一 Grace |
| GPU↔CPU | PCIe | NVLink-C2C | htod/dtoh 门限与 PCIe 机型不可比 |
| NUMA | CPU 节点 | CPU LPDDR 节点 0、1，GPU HBM 也显示为 NUMA 节点 | CPU 测试只绑节点 0、1 |
| 柜内互联 | — | 18 tray 通过 NVLink Switch 组成一个 NVLink 域，跨 tray 走 MNNVL，依赖宿主机 IMEX | 单独的 nvl-domain 配置 |

## 3. 已实施的文件

| 文件 | 内容 |
|-|-|
| `benchmarks/superbenchmark/image/configs/config_gb300_single_node.yaml` | 单机：cublaslt-gemm、nvbandwidth、gpu-copy-bw、gpu-burn、cpu-stream × 4、ib-loopback、nccl-bw × 5（nvlink：allreduce、allgather、reducescatter、alltoall、broadcast） |
| `benchmarks/superbenchmark/image/configs/config_gb300_multi_node.yaml` | 跨节点 IB：ib-traffic（GPU Direct RDMA 单向/双向 + cpu-to-cpu）、nccl-bw × 4（`NCCL_MNNVL_ENABLE=0`） |
| `benchmarks/superbenchmark/image/configs/config_gb300_nvl_domain.yaml` | 柜内 MNNVL：nccl-bw × 5（含 broadcast）+ NVLS 对照（`NCCL_MNNVL_ENABLE=1`） |
| `benchmarks/superbenchmark/image/configs/config_gb300_all_nodes.yaml` | 全部 rack（生产形态）：柜内 NVLink + 柜间 IB，nccl-bw × 5（`NCCL_MNNVL_ENABLE=1`、`NCCL_NVLS_ENABLE=1`）；另有长稳用例 `nccl-bw:soak:allreduce:mnnvl-ib`（不在 enable 中，由 mtest 选择） |
| `benchmarks/superbenchmark/image/configs/config_gb300_disk.yaml` | 磁盘：设备只能由 `--disk` 注入 |
| `benchmarks/superbenchmark/image/configs/rule_gb300.yaml` | 汇总规则：GPU-COMPUTE、GPU-MEM、GPU-STABILITY、CPU-MEM、RDMA-LOCAL、RDMA-MULTINODE、NCCL-NVLINK、NCCL-MNNVL、NCCL-MNNVL-IB、NCCL-IB、DISK |
| `benchmarks/superbenchmark/image/scripts/run-tests.sh` | `-p default|gb300`（arm64 上默认 gb300）、`-t nvl-domain`、`--disk` |

`Dockerfile` 用 `COPY configs/ /workspace/` 打包配置，无需修改。

## 4. 各项改动与依据

### 4.1 GPU 计算

| benchmark | 处理 | 依据 |
|-|-|-|
| `gemm-flops` | 移除 | main 里 `kernel_map[10.3]` 复用 SM80 的 `mma.sync` kernel，测不到 tcgen05 峰值，也没有 FP8/NVFP4。等 microsoft/superbenchmark#883 合入后再评估 |
| `cublaslt-gemm:compute` | 新增：bf16、fp16、fp8e4m3 | 镜像编了 sm_103（CMake arch `80;86;90;100;103`），cuBLASLt 按架构选 kernel。shape 8192³、16384³，开 `enable_autotune` |
| `cublaslt-gemm:fp4` | 新增：fp4e2m1（block-scale VEC16_UE4M3） | 单独一项，避免 fp4 在 SM103 上失败时影响其他精度的结果 |
| `gpu-burn` | `doubles: false` | Blackwell Ultra 的 FP64 硬件大幅削减；保留 `tensor_core: true`，时长 900 秒 |

### 4.2 GPU 显存与拷贝

| benchmark | 处理 | 依据 |
|-|-|-|
| `nvbandwidth` | 新增，跑全部单节点用例，`buffer_size: 512` MiB | 镜像只有单节点二进制（未开 `-DMULTINODE`） |
| `gpu-copy-bw:perf` | 保留 | htod/dtoh 走 NVLink-C2C，门限按 GB300 实测基线定 |
| HBM 带宽 | 不在 SB | 用 DCGM 诊断，不能和 GPU 间 D2D 带宽混用 |

### 4.3 CPU

| benchmark | 处理 | 依据 |
|-|-|-|
| `cpu-stream:numa0` / `numa1` | 每颗 Grace 本地带宽 | `cpu_arch: neo2`（`streamNeo2`，`-mcpu=neoverse-v2`）；核 0–71 绑节点 0，核 72–143 绑节点 1 |
| `cpu-stream:cross-numa` | 跨 Grace 带宽 | 核 0–71，内存绑节点 1 |
| `cpu-stream:node` | 整节点带宽 | 核 0–143，内存绑节点 0、1 |
| `cpu-memory-bw-latency` | 移除 | arm64 上退化为 `cpu_copy`，没有 MLC 延迟矩阵 |
| `cpu-hpl` | 移除 | 只有 x86 实现（AOCC/BLIS） |

`cores`、`numa_mem_nodes` 必须写成字符串列表：worker 的 `sb exec` 用 `' '.join()` 拼参数，整数列表会直接报 `TypeError`（离线校验中发现）。

### 4.4 RDMA 打流

| 项 | x86 配置 | GB300 配置 |
|-|-|-|
| `ib-loopback` | 4 组：`IB_DEVICES=0,4 / 1,5 / 2,6 / 3,7` | 2 组：`IB_DEVICES=0,2` 和 `1,3`，`NUMA_NODES=0,1`。索引指向 IB link-layer 设备列表 |
| `ib-traffic` 方向 | `cpu-to-cpu` | `gpu-to-gpu`（主验收项）+ `cpu-to-cpu`（基线）；另一项 `multinode-bidir` 测 gpu-to-gpu 双向 |
| `ib-traffic` 时延 | — | 单独一项 `multinode-lat`：`ib_write_lat`，`msg_size: 2`，`iters: 20000`，`cpu-to-cpu`。同一项内各命令共用 `msg_size`，若沿用带宽的 8 MiB，测到的主要是传输时间而不是网络时延。`ib_validation` 只输出 `t_avg`，没有中位数和分位数 |
| `ib-traffic` 设备 | 未设置 | `set_ib_devices: true`（每个 rank 取第 local_rank 个 IB 设备）、`gpu_dev: $LOCAL_RANK`、`numa_dev: $((LOCAL_RANK/2))`。`ib_validation` 会把 `LOCAL_RANK` 换成 `OMPI_COMM_WORLD_LOCAL_RANK` 再交给 shell 展开 |
| 模式 | one-to-one，8 rank | one-to-one（全部节点两两配对），4 rank 同时打流覆盖多端口并发 |
| 指定网卡顺序 | — | 若 IB 设备排序与 GPU 亲和不一致，可在 ib-traffic 的 mode `env` 中设置 `IB_DEVICES: mlx5_a,mlx5_b,...`（按 GPU 顺序的设备名） |

### 4.5 NCCL 集合通信

| 项 | x86 配置 | GB300 配置 |
|-|-|-|
| 单节点 | allreduce（8 卡） | allreduce + alltoall（4 卡），`NCCL_NVLS_ENABLE=0` |
| 跨节点 IB | allreduce、allgather | allreduce、allgather、reducescatter、alltoall；`NCCL_MNNVL_ENABLE=0`、`NCCL_NVLS_ENABLE=0`，同柜节点也走 IB |
| 柜内 MNNVL | — | 同 4 种操作，`NCCL_MNNVL_ENABLE=1`；allreduce 另有 `NCCL_NVLS_ENABLE=1` 对照。MNNVL 不可用时 NCCL 直接报错，不会悄悄回退到 IB |
| `NCCL_DEBUG` | `DEBUG`（不是合法级别） | `INFO`，用于在日志中确认实际传输路径 |
| `NCCL_SOCKET_IFNAME` | `eth0`（未生效，`nccl_env` 变量没有被引用） | 不设置，由 NCCL 自动选择（默认排除 lo、docker*） |
| `NCCL_IB_HCA` | 未设置 | 未设置，由 NCCL 按拓扑选最近的 NIC；实机若发现选到 BF3，在 mode `env` 中加 `NCCL_IB_HCA: =mlx5_a,...` |
| 规模 | `node_num: all` | 由 hosts 文件控制：单节点 → 双节点 → 整柜 18 tray → 多柜，分别执行 |

### 4.6 磁盘

x86 配置写死 `/dev/nvme0n1` 并包含写测试，会覆盖盘上数据。GB300 配置的 `block_devices` 为空，只有 `run-tests.sh --disk /dev/xxx` 时才执行，设备通过 `sb run --config-override` 注入。

### 4.7 worker 镜像引用

ISO 构建时按 digest（arm64 `sha256:3828087e…`）拉取，再以 tag `docker.io/superbench/main:cuda13.0` 保存；节点上 `docker load` 不保留 digest，所以 `run-tests.sh` 只能按 tag 引用。不是用 ISO 安装的节点，需要先按 digest 拉取再打同名 tag：

```bash
docker pull docker.io/superbench/main@sha256:3828087ecb299adb72538a2e8abd7a759b7a0170df261cda397f730bfbedadd4
docker tag docker.io/superbench/main@sha256:3828087ecb299adb72538a2e8abd7a759b7a0170df261cda397f730bfbedadd4 \
    docker.io/superbench/main:cuda13.0
```

## 5. 已完成的离线校验

没有 GB300 实机，以下校验在 x86 主机上完成：

- **控制端（sb v0.12.0）**：4 份配置都能加载，YAML anchor 和 `<<` 合并正确展开；runner 为 24 个测试项生成了正确的 `sb exec` / `mpirun` 命令（每节点 4 rank，MNNVL、NVLS 环境变量正确，`--config-override` 正确写入磁盘设备）。
- **worker 端（main@9142bdfe）**：用 executor 相同的逻辑渲染参数，交给各 benchmark 的 argparse 严格解析，24 项全部通过，没有未知参数。
- **汇总规则**：按实际指标命名构造了 16 个指标，`sb result summary` 生成 10 个分类，全部指标都被匹配。
- **run-tests.sh**：用 `sb` 桩程序验证了 default / gb300 / nvl-domain / disk / 错误参数等组合，default profile 的行为与修改前一致。

## 6. 实机确认项

- [ ] `nvidia-smi -L`、`nvidia-smi topo -m`：确认每 tray 4 GPU，GPU↔NIC↔Grace 亲和与第 2 节一致。
- [ ] `ibdev2netdev`、`ibstat`：IB link-layer 设备恰好是 4 张 CX8，按名称排序后与 GPU 0–3 一一对应；BF3 为 Ethernet。否则调整 `IB_DEVICES`、ib-loopback 索引。
- [ ] `lscpu`、`numactl -H`：Grace 核编号为 0–71 / 72–143，LPDDR 为 NUMA 节点 0、1。否则修改 cpu-stream 的核列表和节点。
- [ ] `nvidia-peermem` 已加载（或 dma-buf 可用），ib-traffic gpu-to-gpu 实际走 GPU Direct RDMA。SB 只传 `--use_cuda=<gpu>`。
- [ ] cublaslt-gemm 的 `fp4e2m1` 在 SM103 上可运行。
- [ ] main:cuda13.0 中 nccl-tests（sm_100 cubin）在 SM103 上可运行。
- [ ] worker 容器（`sb deploy` 以 `--privileged` 启动）中可见 `/dev/nvidia-caps-imex-channels`，nvl-domain 配置能在整柜跑通。
- [ ] NCCL `INFO` 日志中 IB 配置走 NET/IB、nvl-domain 配置走 MNNVL，NCCL 没有选到 BF3。
- [ ] 测试盘设备名，以及 fio 的容量、队列深度门限。
- [ ] 首批机器实测后，为 `rule_gb300.yaml` 各分类建立基线门限（实测前用 OEM 同型号交付指标）。

## 7. 已知问题

- **sbcli 镜像中 `sb result summary` 失败（已修复）**：sb v0.12.0 只要求 `pandas>=1.1.5`，新构建会装上 pandas 3.x，汇总时报 `Cannot specify both 'axis' and 'index'/'columns'`，生成不了 Excel 报告。sbcli `Dockerfile` 已固定 `pandas<3`（实测装上 2.3.3，default 和 gb300 的汇总都正常）。
- **汇总报告指标名被截断**：v0.12.0 在 `aggregate: True` 时用 `str.strip()` 去 rank 后缀，会把指标名开头的字符一起删掉（如 `gpu-copy-bw` 显示为 `-copy-bw`）。数值正确，原有 `rule_all.yaml` 也有同样现象。
- **gemm-flops 峰值**：依赖 #883 合入并重建 worker 镜像。

## 8. 不在 SuperBench 内处理的测试

| 测试 | 归属 | 原因 |
|-|-|-|
| 多节点 nvbandwidth（`-DMULTINODE=1`）、NVLoom | 宿主机 DCGM `dcgmi mndiag`（`mnnvbandwidth` / `nvloom`，验收计划 S7b）；手工排查用 `imex-bench.sh` T2 | SB 镜像未开启 MULTINODE，SB wrapper 也不支持按 GPU 起 MPI rank；DCGM 4.7 multinode 包已带 GB300 可用的二进制 |
| fabric IPC 正确性、MNNVL 开/关对比 | moe-bench 镜像 / 宿主机 `imex-bench.sh` T1、T3 | 数据面测试，依赖宿主机 IMEX 已正常运行 |
| IMEX 状态检查（L0–L3） | 宿主机 `imex-check.sh` | IMEX daemon 是宿主机服务 |
| Hybrid-EP、DeepGEMM | moe-bench 镜像 | 独立镜像 |
| stress-ng、CPU/内存长稳 | 宿主机 | SB 没有 stress-ng 测试；由统一长稳脚本编排，并采集 RAS、Xid、温度 |
| Grace CPU HPL | 宿主机（可选） | SB 只有 x86 实现；GB300 的 CPU FP64 与 GPU 业务关联小，优先级低 |
