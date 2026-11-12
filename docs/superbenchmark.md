# SuperBench CLI 完整测试使用指南

## 一、概述

SuperBench (sb) 是一个用于 AI 基础设施性能测试的工具，支持单机测试、多机测试和全量测试。测试完成后，可以通过 `sb` 子命令提取和分析测试报告。

## 二、准备工作

### 2.1 文件清单

进行测试前，需要准备以下文件：

| 文件类型 | 文件名 | 用途 | 位置 |
|---------|--------|------|------|
| 主机清单 | `hosts` | Ansible inventory 文件，定义被测主机列表 | 需自行准备并挂载 |
| SSH 私钥 | `id_rsa` | 连接被测主机的 SSH 密钥 | 需挂载到容器 |

**镜像内置配置文件（位于 `/workspace/`）：**

| 文件类型 | 文件名 |
|---------|--------|
| 测试配置 | `config_single_node.yaml` |
| 测试配置 | `config_multi_node.yaml` |
| 测试配置 | `config_all.yaml` |
| 测试配置（GB300） | `config_gb300_single_node.yaml`、`config_gb300_multi_node.yaml`、`config_gb300_nvl_domain.yaml`、`config_gb300_sharp.yaml`（testkit SHARP 测试项使用）、`config_gb300_disk.yaml` |
| 汇总规则 | `rule_single_node.yaml` |
| 汇总规则 | `rule_multi_node.yaml` |
| 汇总规则 | `rule_all.yaml` |
| 汇总规则（GB300） | `rule_gb300.yaml` |

### 2.2 控制节点 sbcli 镜像准备

使用 Docker 容器作为控制节点来执行 `sb` 命令，需要准备 sbcli 镜像并满足以下依赖条件。

#### 前置依赖

1. **SSH 连接要求**
   - Ansible 控制节点能够通过 SSH 连接到每个被测主机
   - 确保 SSH 密钥已配置，或密码登录可用

2. **被测主机环境要求**
   - 每个被测主机已安装 Docker
   - 被测主机已加载 SuperBench 镜像（需 load 到主机上）：
     - x86（H100/H200/B300 等）：`docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9`
     - GB300（arm64）：`docker.io/superbench/main:cuda13.0`（GB300 ISO 已预置；v0.12.0 不支持 GB300 的 sm_103）

#### 准备 sbcli 镜像

使用 GitHub Container Registry 上的镜像：

```bash
# 拉取镜像
docker pull ghcr.io/daocloud/metal-deployer/sbcli:latest

# 验证镜像
docker run --rm ghcr.io/daocloud/metal-deployer/sbcli:latest sb version
```

sbcli 为多架构镜像（`linux/amd64`、`linux/arm64`），可直接在 GB300（Grace）节点上运行。

#### 准备必要文件

在运行容器前，需要准备以下文件并挂载到容器中：

| 文件 | 用途 | 挂载路径示例 |
|-----|------|-------------|
| `hosts` | Ansible inventory 文件，定义被测主机列表 | `/workspace/hosts` |
| SSH 私钥 | 用于连接到被测主机的 SSH 密钥 | `/root/.ssh/id_rsa` |
| SSH config (可选) | SSH 配置文件 | `/root/.ssh/config` |

**hosts 文件示例：**

```ini
[all]
node1 ansible_host=192.168.1.101
node2 ansible_host=192.168.1.102
node3 ansible_host=192.168.1.103
node4 ansible_host=192.168.1.104

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_rsa
ansible_ssh_extra_args='-o StrictHostKeyChecking=no'
```

#### 运行 sbcli 容器并执行部署

使用以下命令启动容器并执行 `sb deploy`：

```bash
# 启动控制面的容器：
docker run -it \
  -v $(pwd)/hosts:/workspace/hosts \
  -v ~/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v ~/.ssh/config:/root/.ssh/config:ro \
  --entrypoint /bin/bash \
  ghcr.io/daocloud/metal-deployer/sbcli:latest

# 在容器内执行（x86 被测节点）
sb deploy -f hosts --no-image-pull -i docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9
# GB300 被测节点
sb deploy -f hosts --no-image-pull -i docker.io/superbench/main:cuda13.0
```

参数说明：
- `-f hosts`：指定 inventory 文件
- `--no-image-pull`：跳过镜像拉取（假设被测主机已预加载镜像）
- `-i <镜像>`：指定被测主机上已加载的 SuperBench 镜像（x86 / GB300 取值见上）

该命令会在被测机器上启动 superbench 容器。

#### 容器与宿主机的软件版本

SuperBench 通常使用 worker 镜像自带的 CUDA runtime、cuBLAS、cuDNN、NCCL、MPI 和 UCX。除非显式挂载或覆盖这些库，升级宿主机 CUDA Toolkit 或 NCCL 不会更新容器内版本；宿主机也不必安装相同版本的 Toolkit 才能运行预编译的 GPU 容器。

容器仍共享宿主机内核，依赖宿主机 GPU 驱动、网卡/RDMA 内核模块和 NVIDIA Container Toolkit。驱动必须满足镜像要求，容器内 RDMA 用户态组件需兼容宿主机驱动。GB300 跨节点 NVLink 还依赖宿主机 IMEX、交换托盘 NVOS/NMX 和正确的设备权限，不能仅凭镜像 CUDA 标签判断兼容性。

宿主机 DCGM 用于宿主机监控和诊断，不会替换容器内 DCGM。容器若连接宿主机 hostengine，需单独核对客户端兼容性。GB300 manifest 的 DCGM 4.7.1 升级不改变 SuperBench 镜像；容器测试结果也不能证明宿主机 Toolkit/NCCL 或 DCGM 诊断全部可用。

#### 注意事项

1. **SSH 密钥权限**：确保挂载的 SSH 私钥在容器内具有正确权限（600）
2. **网络连通性**：确保 Ansible 控制节点能够访问所有被测主机的 SSH 端口
3. **Docker 权限**：被测主机的用户需要有执行 Docker 命令的权限
4. **镜像预加载**：被测主机必须预先加载对应架构的 SuperBench 镜像（见上）
5. **GB300 限制**：main 镜像中 `gemm-flops` 在 GB300 上只跑 Ampere 时代的 kernel，结果不代表峰值算力（等 microsoft/superbenchmark#883）；`cpu-memory-bw-latency`、`cpu-hpl` 仅支持 x86；内存带宽用 `nvbandwidth`。GB300 请使用 `config_gb300_*.yaml`（见 3.4），不要直接套用 x86 的 8 卡配置

## 三、测试场景与配置文件

### 3.1 单机测试 (/workspace/config_single_node.yaml)

适用于单节点 GPU 服务器内的全面性能测试, 大概 2 小时左右。

**测试内容：**
- CPU: `cpu-memory-bw-latency`, `cpu-hpl`
- GPU: `gpu-copy-bw`, `gpu-burn`, `kernel-launch`, `gemm-flops`
- 存储: `disk-benchmark`
- 网络: `ib-loopback`, `nccl-bw:nvlink`

**关键配置示例：**
```yaml
version: v0.13
superbench:
  enable:
    - cpu-memory-bw-latency
    - cpu-hpl
    - disk-benchmark
    - gpu-copy-bw:perf
    - gpu-burn
    - ib-loopback:1G
    - nccl-bw:bw:allreduce:nvlink
  monitor:
    enable: true
    sample_duration: 1
    sample_interval: 10
```

### 3.2 多机测试 (/workspace/config_multi_node.yaml)

适用于多节点集群的互联性能测试，大概 2 小时左右。

**测试内容：**
- IB 网络: `ib-traffic:perftest:multinode`
- NCCL 跨机: `nccl-bw:allreduce:ib`, `nccl-bw:allgather:ib`

**关键配置示例：**
```yaml
version: v0.13
superbench:
  enable:
    - ib-traffic:perftest:multinode
    - nccl-bw:bw:allreduce:ib
    - nccl-bw:bw:allgather:ib
  benchmarks:
    ib-traffic:perftest:multinode:
      timeout: 6000
      modes:
        - name: mpi
          proc_num: 8
          node_num: all  # 关键：使用所有节点
          mca:
            pml: ob1
            btl: ^openib
            plm_rsh_args: '"-F /root/.ssh/config -i /root/.ssh/key"'
```

### 3.3 全量测试 (/workspace/config_all.yaml)

适用于全面的性能和模型测试，包含所有单机+多机测试项以及模型测试，大概 8 小时。

**测试内容：**
- 所有单机测试项
- 所有多机测试项
- 模型测试: `dist-inference`, `model-benchmarks`, `megatron-gpt` 等。

**注意：** 模型测试需要较长的执行时间，建议单独运行。

### 3.4 GB300 测试 (/workspace/config_gb300_*.yaml)

GB300 每个 tray 为 4 × B300、2 × Grace、4 × ConnectX-8，与 x86 8 卡机型的 GPU 数、NIC 编号、CPU 架构都不同，因此使用单独的配置组。`run-tests.sh -p gb300` 自动选用这些配置和 `rule_gb300.yaml`；sbcli 运行在 arm64 上时默认就是 gb300。

| 配置 | 用途 | 关键点 |
|-|-|-|
| `config_gb300_single_node.yaml` | 单机 | `proc_num: 4`；`cublaslt-gemm` 替代 `gemm-flops`；`cpu-stream`（`cpu_arch: neo2`，按 Grace 核和 LPDDR NUMA 节点绑定）替代 MLC/HPL；`nvbandwidth`；`gpu-burn` 不测 FP64；`ib-loopback` 2 组 |
| `config_gb300_multi_node.yaml` | 跨节点 IB | `ib-traffic` 测 gpu-to-gpu（GPU Direct RDMA，单向/双向），另测 cpu-to-cpu 时延（2 字节）；`set_ib_devices` 只选 IB link-layer 设备；NCCL 4 种集合通信，`NCCL_MNNVL_ENABLE=0` |
| `config_gb300_nvl_domain.yaml` | 柜内 MNNVL | hosts 必须在同一 NVLink 域；`NCCL_MNNVL_ENABLE=1`，不满足时直接失败而不是回退到 IB |
| `config_gb300_sharp.yaml` | NVLink SHARP / IB SHARP | 每组 NCCL 设置一个 nccl-bw 条目（U-A/U-B 柜内，O-A~O-D 柜间），均显式设置 `NCCL_NVLS_ENABLE`；O-C/O-D 加载 HPC-X SHARP 插件。由 testkit `sharp.*` 测试项按组选择 |
| `config_gb300_disk.yaml` | 磁盘 | 只在 `run-tests.sh --disk <设备>` 时执行，设备通过 `--config-override` 注入 |

手动执行时，磁盘配置必须显式指定测试盘：

```bash
sb run -f hosts --config-file /workspace/config_gb300_disk.yaml \
    --config-override "superbench.benchmarks.disk-benchmark.parameters.block_devices=[/dev/nvme1n1]"
```

硬件假设、实机确认项和不在 SuperBench 内处理的测试见 [superbench-gb300.md](./superbench-gb300.md)。

## 四、测试执行流程

### 4.1 本地单机测试

```bash
# 使用 sbcli 容器内的配置
sb run -f /workspace/hosts \
       --config-file /workspace/config_single_node.yaml \
       --output-dir /workspace/outputs/single-node
```

输出报告会存放在 `/workspace/outputs/single-node` 目录下。

### 4.2 多机测试

```bash
sb run -f /workspace/hosts \
       --config-file /workspace/config_multi_node.yaml \
       --output-dir /workspace/outputs/multi-node \
       --get-info  # 同时收集系统信息
```

输出报告会存放在 `/workspace/outputs/multi-node` 目录下。

### 4.3 全量测试

```bash
sb run -f /workspace/hosts \
       --config-file /workspace/config_all.yaml \
       --output-dir /workspace/outputs/full-test \
       --get-info
```

输出报告会存放在 `/workspace/outputs/full-test` 目录下。

## 五、结果提取与分析

测试完成后，在 `${output-dir}` 目录下会生成 `results-summary.jsonl` 文件，这是后续分析的基础数据文件。

### 5.1 生成结果汇总报告 (sb result summary)

根据规则文件生成可读的测试汇总报告。

```bash
# 单机测试汇总 - Excel 格式
sb result summary \
    --data-file /workspace/outputs/single-node/results-summary.jsonl \
    --rule-file /workspace/rule_single_node.yaml \
    --output-file-format excel \
    --output-dir /workspace/reports/single-node

# 多机测试汇总 - Excel 格式
sb result summary \
    --data-file /workspace/outputs/multi-node/results-summary.jsonl \
    --rule-file /workspace/rule_multi_node.yaml \
    --output-file-format excel \
    --output-dir /workspace/reports/multi-node

# 全量测试汇总 - Excel 格式
sb result summary \
    --data-file /workspace/outputs/full-test/results-summary.jsonl \
    --rule-file /workspace/rule_all.yaml \
    --output-file-format excel \
    --output-dir /workspace/reports/full-test
```

**输出文件：**

- Excel: `/workspace/reports/{test-type}/results-summary.xlsx`

##  六、常见问题

**Q: 测试失败后如何查看日志？**
A: 在 `${output-dir}/` 目录下查看各节点的详细日志文件。

**Q: 如何只运行特定测试项？**
A: 使用 `--config-override` 参数：
```bash
sb run --host-list localhost --config-override 'superbench.enable=["gpu-burn", "nccl-bw"]' 
```

**Q: 如何跳过 Docker 直接运行？**
A: 使用 `--no-docker` 参数，需确保本地已安装 sb 环境。

**Q: 诊断规则中的 criteria 如何编写？**
A: 使用 Python lambda 函数表达式：
- `'lambda x:x<-0.1'` - 性能下降超过10%
- `'lambda x:x>0'` - 失败检测（return_code > 0）
- `'lambda x:x>100'` - 延迟超过100ms

## 十、参考文档

- [SuperBench CLI 详细文档](https://microsoft.github.io/superbenchmark/docs/introduction)
- [配置文件说明](https://microsoft.github.io/superbenchmark/docs/getting-started/configuration)
- [结果汇总说明](https://microsoft.github.io/superbenchmark/docs/user-tutorial/result-summary)
- [数据诊断说明](https://microsoft.github.io/superbenchmark/docs/user-tutorial/data-diagnosis)
