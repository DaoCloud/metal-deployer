# SuperBench 一键测试指南

本文档介绍如何使用 sbcli 镜像一键执行 GPU 集群的性能测试。

## 一、环境准备

### 1.1 前置依赖

在开始测试前，请确保以下环境已准备就绪：

| 依赖项 | 要求 | 说明 |
|--------|------|------|
| **控制节点** | Docker 已安装 | 用于运行 sbcli 控制容器 |
| **被测节点** | Docker 已安装 | 所有被测主机需要 Docker 环境 |
| **被测节点镜像** | 已加载镜像 | 被测主机需预先加载 `docker.m.daocloud.io/superbench/superbench:v0.12.0-cuda12.9` |
| **SSH 连接** | 密钥认证 | 控制节点能通过 SSH 密钥连接到所有被测节点 |

### 1.2 镜像准备

**拉取 sbcli 控制镜像：**

```bash
docker pull ghcr.io/daocloud/metal-deployer/sbcli:latest
```

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
- `-t, --test-case CASE`：测试类型，可选 `single-node`、`multi-node`、`all`（默认执行 single-node + multi-node）

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
