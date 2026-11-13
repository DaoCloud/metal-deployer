# SuperBench 一键测试指南

本文档介绍如何使用 sbcli 镜像一键执行 GPU 集群的性能测试。

## 一、环境准备

### 1.1 前置依赖

在开始测试前，请确保以下环境已准备就绪：

| 依赖项 | 要求 | 说明 |
|--------|------|------|
| **控制节点** | Docker 已安装 | 用于运行 sbcli 控制容器 |
| **被测节点** | Docker 已安装 | 所有被测主机需要 Docker 环境 |
| **被测节点镜像** | 已加载镜像 | x86：`docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9`；GB300（arm64）：`docker.io/superbench/main:cuda13.0`（GB300 ISO 已预置） |
| **SSH 连接** | 密钥认证 | 控制节点能通过 SSH 密钥连接到所有被测节点 |

### 1.2 镜像准备

**拉取 sbcli 控制镜像：**

```bash
docker pull ghcr.io/daocloud/metal-deployer/sbcli:latest
```

sbcli 镜像同时提供 `linux/amd64` 和 `linux/arm64`，`docker pull` 会按本机架构自动选择，GB300（Grace）节点上可直接使用。

> **GB300 注意**：SuperBench 正式版 v0.12.0 不支持 GB300（CC 10.3 / sm_103），GB300 被测节点使用 main 分支 CUDA 13.0 镜像，并使用 GB300 专用配置（`-p gb300`，见 5.3）。`run-tests.sh` 在 arm64 上默认使用 gb300 profile 和该镜像；若用 x86 控制节点测试 GB300，需加 `-p gb300`。在该镜像中，`gemm-flops` 在 GB300 上只跑 Ampere 时代的 kernel，结果不代表峰值算力，GB300 配置改用 `cublaslt-gemm`；`cpu-memory-bw-latency`、`cpu-hpl` 仅支持 x86，GB300 配置改用 `cpu-stream`。改动依据和实机待确认项见 [docs/superbench-gb300.md](../../docs/superbench-gb300.md)。

**在被测节点加载测试镜像：**

```bash
# 在每个被测节点上执行
docker load -i superbench-v0.12.0-cuda12.9.tar
# 或从镜像仓库拉取（如果能访问外网）
docker pull docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9
```

## 二、准备测试文件

### 2.1 创建 hosts 文件

hosts 文件是 Ansible inventory 格式，定义被测主机列表：

```bash
mkdir -p ~/sbtest
cd ~/sbtest

cat > hosts << 'INNEREOF'
[all]
node1 ansible_host=192.168.1.101
node2 ansible_host=192.168.1.102
node3 ansible_host=192.168.1.103
node4 ansible_host=192.168.1.104

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_rsa
ansible_ssh_extra_args='-o StrictHostKeyChecking=no'
INNEREOF
```

### 2.2 准备 SSH 密钥

确保控制节点有 SSH 私钥，且对应公钥已分发到所有被测节点：

```bash
# 测试 SSH 连接
ssh -i ~/.ssh/id_rsa root@192.168.1.101 "echo '连接成功'"
```

## 三、一键执行测试

### 3.1 启动控制容器

使用以下命令启动 sbcli 容器，挂载必要的文件：

```bash
docker run -it \
  -v ~/sbtest/hosts:/workspace/hosts \
  -v ~/.ssh:/root/.ssh:ro \
  -v ~/sbtest/results:/workspace/outputs \
  --entrypoint /bin/bash \
  ghcr.io/daocloud/metal-deployer/sbcli:latest
```

**挂载说明：**
- `~/sbtest/hosts:/workspace/hosts` - hosts 文件挂载到容器
- `~/.ssh:/root/.ssh:ro` - SSH 密钥挂载（只读）
- `~/sbtest/results:/workspace/outputs` - **关键：将容器内输出目录挂载到宿主机**，方便测试完成后查看结果

### 3.2 执行一键测试脚本

进入容器后，执行内置的一键测试脚本：

```bash
# 执行默认测试（single-node + multi-node）
/workspace/scripts/run-tests.sh

# 仅执行单机测试
/workspace/scripts/run-tests.sh -t single-node

# 仅执行多机测试
/workspace/scripts/run-tests.sh -t multi-node

# 执行全部测试（包含模型测试，约 8 小时）
/workspace/scripts/run-tests.sh -t all

# GB300：单机 + 跨节点 IB（sbcli 在 arm64 上时可省略 -p gb300）
/workspace/scripts/run-tests.sh -p gb300

# GB300：柜内 MNNVL 集合通信（hosts 中的节点必须在同一 NVLink 域）
/workspace/scripts/run-tests.sh -p gb300 -t nvl-domain

# GB300：单机 + 跨节点 IB + MNNVL，并对指定测试盘做磁盘测试（会覆盖写入该盘）
/workspace/scripts/run-tests.sh -p gb300 -t all --disk /dev/nvme1n1

# 查看帮助
/workspace/scripts/run-tests.sh --help
```

**脚本功能：**
1. 部署 SuperBench 到所有被测节点
2. 执行指定类型的测试（single-node、multi-node、all）
3. 自动合并结果并生成统一汇总报告（Excel 格式）

**脚本参数：**
- `-h, --hosts FILE`：hosts 文件路径（默认 `/workspace/hosts`）
- `-o, --output DIR`：输出目录（默认 `/workspace/outputs`）
- `-p, --profile PROFILE`：配置组，`default`（x86 8 卡机型）或 `gb300`。默认按 sbcli 所在机器架构选择：x86 为 `default`，arm64 为 `gb300`
- `-t, --test-case CASE`：测试类型，可选 `single-node`、`multi-node`、`all`（默认执行 single-node + multi-node）。gb300 另支持 `nvl-domain`；gb300 的 `all` 为 single-node + multi-node + nvl-domain，不含模型测试
- `-i, --worker-image IMG`：被测节点 SuperBench 镜像。默认按 profile 选择：`default` 为 `docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9`，`gb300` 为 `docker.io/superbench/main:cuda13.0`。也可用环境变量 `WORKER_IMAGE`（兼容旧变量 `SBCLI_IMAGE`）指定
- `--disk DEVS`：仅 gb300。磁盘测试使用的块设备，逗号分隔（如 `/dev/nvme1n1,/dev/nvme2n1`）。**会覆盖写入设备数据**，只能指定已确认可清空的测试盘；不指定则不跑磁盘测试

## 四、查看测试结果

### 4.1 容器内查看

测试过程中，可以在容器内查看进度：

```bash
# 查看输出目录结构
ls -la /workspace/outputs/

# 查看测试日志
tail -f /workspace/outputs/{timestamp}/single-node/sb_stdout.txt
```

### 4.2 宿主机查看结果

由于启动容器时已将 `/workspace/outputs` 挂载到宿主机的 `~/sbtest/results`，测试完成后可直接在宿主机查看：

```bash
# 退出容器后，在宿主机执行
ls -la ~/sbtest/results/

# 查看生成的统一汇总报告
ls -la ~/sbtest/results/{timestamp}/reports/superbench-test-summary.xlsx
```

### 4.3 测试结果结构

```
~/sbtest/results/
└── {timestamp}/                    # 时间戳命名的测试批次
    ├── deploy/                     # 部署日志
    ├── single-node/                # 单机测试结果（如果执行了）
    │   ├── results-summary.jsonl   # 原始数据
    │   └── sb_stdout.txt           # 测试日志
    ├── multi-node/                 # 多机测试结果（如果执行了）
    │   ├── results-summary.jsonl
    │   └── sb_stdout.txt
    ├── all/                        # 全量测试结果（如果执行了 -t all）
    │   ├── results-summary.jsonl
    │   └── sb_stdout.txt
    ├── nvl-domain/                 # GB300 MNNVL 测试结果（如果执行了）
    ├── disk/                       # GB300 磁盘测试结果（如果指定了 --disk）
    └── reports/                    # 汇总报告
        └── superbench-test-summary.xlsx    # 统一汇总报告（包含所有测试类型）
```

## 五、测试场景说明

### 5.1 单机测试 (config_single_node.yaml)

- **时长**：约 2 小时
- **内容**：单节点内部性能测试
  - CPU: `cpu-memory-bw-latency`, `cpu-hpl`
  - GPU: `gpu-copy-bw`, `gpu-burn`, `kernel-launch`, `gemm-flops`
  - 存储: `disk-benchmark`
  - 网络: `ib-loopback`, `nccl-bw:nvlink`

### 5.2 多机测试 (config_multi_node.yaml)

- **时长**：约 2 小时
- **内容**：多节点互联性能测试
  - IB 网络: `ib-traffic:perftest:multinode`
  - NCCL 跨机: `nccl-bw:allreduce:ib`, `nccl-bw:allgather:ib`

### 5.3 GB300 测试（`-p gb300`）

按每个 tray 4 × B300、2 × Grace（每颗 72 核）、4 × ConnectX-8（每卡单口 800G XDR，rail 对齐）编写，汇总规则为 `rule_gb300.yaml`。硬件假设需在实机上确认，见 [docs/superbench-gb300.md](../../docs/superbench-gb300.md) 第 5 节。

| 测试类型 | 配置文件 | 内容 |
|-|-|-|
| `single-node` | `config_gb300_single_node.yaml` | GPU 算力 `cublaslt-gemm`（bf16/fp16/fp8，fp4 单独一项）；拷贝带宽 `nvbandwidth`、`gpu-copy-bw`；`gpu-burn`（不测 FP64）；Grace 内存带宽 `cpu-stream`（numa0、numa1、跨 NUMA、整节点）；`ib-loopback`；`nccl-bw` allreduce/alltoall（4 卡） |
| `multi-node` | `config_gb300_multi_node.yaml` | `ib-traffic`：GPU Direct RDMA（gpu-to-gpu，单向 + 双向）和 cpu-to-cpu 基线，另有 cpu-to-cpu 时延（`ib_write_lat`，2 字节）；`nccl-bw` allreduce/allgather/reducescatter/alltoall，强制走 IB（`NCCL_MNNVL_ENABLE=0`） |
| `nvl-domain` | `config_gb300_nvl_domain.yaml` | `nccl-bw` allreduce（含 NVLS 对照）/allgather/reducescatter/alltoall，强制走 MNNVL（`NCCL_MNNVL_ENABLE=1`）。hosts 中的节点必须在同一 NVLink 域，宿主机 IMEX 已正常运行 |
| （testkit `sharp.*`） | `config_gb300_sharp.yaml` | `nccl-bw:sharp:<op>:<组>`：柜内 U-A（NVLS 关）/ U-B（NVLink SHARP），柜间 O-A~O-D（无 SHARP / NVLSTree / IB SHARP / 两级）；由 testkit 按组选择条目，`run-tests.sh` 不使用 |
| `--disk` | `config_gb300_disk.yaml` | `disk-benchmark`，设备由 `--disk` 传入，含顺序写和随机写 |

GB300 配置不包含 `gemm-flops`、`cpu-memory-bw-latency`、`cpu-hpl`。多节点 nvbandwidth、IMEX 检查、stress-ng 不在 SuperBench 中执行。

## 六、手动执行单项测试（可选）

如果只需要执行特定测试，可以手动运行 sb 命令：

```bash
# 仅执行单机测试
sb run -f /workspace/hosts \
       --config-file /workspace/config_single_node.yaml \
       --output-dir /workspace/outputs/single-node

# 仅执行多机测试
sb run -f /workspace/hosts \
       --config-file /workspace/config_multi_node.yaml \
       --output-dir /workspace/outputs/multi-node \
       --get-info

# 生成汇总报告
sb result summary \
    --data-file /workspace/outputs/single-node/results-summary.jsonl \
    --rule-file /workspace/rule_single_node.yaml \
    --output-file-format excel \
    --output-dir /workspace/reports
```

## 七、常见问题

**Q: 测试失败后如何查看日志？**
A: 在输出目录的 `${output-dir}/sb_stdout.txt` 文件中查看详细日志。

**Q: 如何只运行特定测试项？**
A: 使用 `--config-override` 参数覆盖配置：
```bash
sb run -f /workspace/hosts \
       --config-override 'superbench.enable=["gpu-burn", "nccl-bw"]' \
       --output-dir /workspace/outputs/custom
```

**Q: 测试过程中断后如何恢复？**
A: 测试不支持断点续传，需要重新执行。建议单次执行完整的 `run-tests.sh`。

**Q: 如何修改测试配置？**
A: 可以复制镜像内的配置文件到宿主机修改后挂载：
```bash
# 从容器复制配置出来
docker run --rm ghcr.io/daocloud/metal-deployer/sbcli:latest \
  cat /workspace/config_single_node.yaml > custom_config.yaml

# 修改后挂载到容器使用
docker run -it \
  -v $(pwd)/custom_config.yaml:/workspace/custom_config.yaml \
  ...
```

## 八、参考文档

- [SuperBench CLI 详细文档](https://microsoft.github.io/superbenchmark/docs/introduction)
- [配置文件说明](https://microsoft.github.io/superbenchmark/docs/getting-started/configuration)
- [结果汇总说明](https://microsoft.github.io/superbenchmark/docs/user-tutorial/result-summary)
