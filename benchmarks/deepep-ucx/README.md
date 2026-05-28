# DeepEP + UCX 性能测试工具

本目录包含 DeepEP 和 UCX 的 GPU 通信性能测试工具使用文档.

## 测试使用指南

### DeepEP 测试

DeepEP 测试用于验证 GPU 节点间的高速通信性能，包括：
- **Intranode 测试**：单节点内 GPU 间通信
- **Low Latency 测试**：低延迟通信模式
- **Internode 测试**：多节点间 GPU 通信

#### 前置条件

1. **NVIDIA GPU 驱动**：所有节点需安装 NVIDIA 驱动
2. **Docker 和 NVIDIA Container Toolkit**：支持 GPU 容器运行
3. **SSH 免密登录**：节点间需配置 SSH 免密（用于多节点测试）
4. **hosts 文件**：包含所有测试节点的管理 IP

#### 运行容器

```bash
# 单节点测试
docker run --rm --gpus all \
  -e RUN_INTERNODE=false \
  -e RUN_INTRANODE=true \
  -e RUN_LOW_LATENCY=true \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testDeepEP

# 多节点测试（需要 hosts 文件）
docker run --rm --gpus all \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RUN_INTERNODE=true \
  -e RUN_INTRANODE=true \
  -e RUN_LOW_LATENCY=true \
  -e SSH_USER=root \
  -e SSH_PORT=22 \
  -e INTERNODE_NUM_PROCESSES=8 \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testDeepEP
```

#### 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RUN_INTRANODE` | `true` | 是否运行单节点测试 |
| `RUN_LOW_LATENCY` | `true` | 是否运行低延迟测试 |
| `RUN_INTERNODE` | `false` | 是否运行多节点测试 |
| `HOSTS_FILE` | - | 节点列表文件路径 |
| `SSH_USER` | `root` | SSH 用户名 |
| `SSH_PORT` | `2022` | SSH 端口 |
| `INTERNODE_NUM_PROCESSES` | 自动检测 | 每节点 GPU/进程数 |
| `REPORT_FILE` | `/tmp/deepep_benchmark_results.json` | 报告输出路径 |

#### 查看结果

```bash
# 查看 JSON 格式报告
cat /tmp/deepep_benchmark_results.json | jq

# 关键指标
# - intranode: 单节点带宽和延迟
# - low_latency: 低延迟模式性能
# - internode: 多节点间通信性能
```

### UCX 测试

UCX 测试用于验证 RDMA 网络性能，支持 InfiniBand 和 Ethernet 模式。

#### 前置条件

1. **RDMA 设备**：节点配备 RDMA 网卡（如 Mellanox ConnectX）
2. **网络配置**：节点间 RDMA 网络互通
3. **Docker 权限**：容器需要访问 RDMA 设备

#### 运行容器

```bash
# InfiniBand 模式测试
docker run --rm --privileged \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=infiniband \
  -e SAME_RAIL_ONLY=true \
  -e NUM_ITERS=10000 \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testUcx

# Ethernet 模式测试
docker run --rm --privileged \
  -v /path/to/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=ethernet \
  -e SAME_NETWORK_TRACK=true \
  -e NUM_ITERS=5000 \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testUcx
```

#### 环境变量说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOSTS_FILE` | - | 节点列表文件路径（必须） |
| `SSH_USER` | `root` | SSH 用户名 |
| `SSH_PORT` | `2022` | SSH 端口 |
| `RDMA_MODE` | `infiniband` | RDMA 模式：`infiniband` 或 `ethernet` |
| `SAME_NETWORK_TRACK` | `true` | 要求客户端/服务器在同一子网 |
| `SAME_RAIL_ONLY` | `true` | 仅测试相同 Rail 设备 |
| `TESTS` | `tag_lat tag_bw` | 测试类型：延迟和/或带宽 |
| `NUM_ITERS` | `10000` | 每测试迭代次数 |
| `LAT_MSG_SIZE` | `64` | 延迟测试消息大小（字节） |
| `BW_MSG_SIZE` | `8388608` | 带宽测试消息大小（字节） |
| `BASE_PORT` | `13337` | 基准端口 |
| `REPORT_FILE` | `/tmp/ucx_rdma_benchmark_report.json` | 报告输出路径 |

#### 查看结果

```bash
# 查看 JSON 格式报告
cat /tmp/ucx_rdma_benchmark_report.json | jq

# 关键指标
# - tag_lat: 点对点延迟（微秒）
# - tag_bw: 点对点带宽（MB/s 或 GB/s）
```

---

## 完整测试示例

### 准备 hosts 文件

```bash
# 创建 hosts 文件，每行一个节点管理 IP
cat > /tmp/hosts <<EOF
192.168.1.101
192.168.1.102
192.168.1.103
192.168.1.104
EOF
```

### 单节点 DeepEP 测试

```bash
docker run --rm --gpus all \
  -e RUN_INTERNODE=false \
  -e RUN_INTRANODE=true \
  -e RUN_LOW_LATENCY=true \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testDeepEP
```

### 多节点 RDMA 带宽测试

```bash
# 4 节点 InfiniBand 测试
docker run --rm --privileged \
  -v /tmp/hosts:/hosts \
  -e HOSTS_FILE=/hosts \
  -e RDMA_MODE=infiniband \
  -e TESTS=tag_bw \
  -e NUM_ITERS=5000 \
  ghcr.io/daocloud/metal-deployer/deepep-ucx:latest \
  testUcx
```

---

## 故障排查

### DeepEP 测试失败

1. **检查 GPU 驱动**：
   ```bash
   nvidia-smi
   ```

2. **检查 NVSHMEM 环境**：
   ```bash
   ls -la /opt/nvshmem/
   ```

3. **SSH 连接问题**：
   ```bash
   # 测试 SSH 免密登录
   ssh -p 2022 root@node-ip "nvidia-smi"
   ```

### UCX 测试失败

1. **检查 RDMA 设备**：
   ```bash
   # 在容器内执行
   ibstat
   ibv_devinfo
   ```

2. **网络连通性**：
   ```bash
   # 测试 RDMA 连通性
   ib_write_bw  # 服务器端
   ib_write_bw <server-ip>  # 客户端
   ```

3. **端口冲突**：
   ```bash
   # 修改基准端口
   -e BASE_PORT=23333
   ```

---

## 参考链接

- [DeepEP GitHub](https://github.com/deepseek-ai/DeepEP)
- [DeepGEMM GitHub](https://github.com/deepseek-ai/DeepGEMM)
- [UCX Documentation](https://openucx.readthedocs.io/)
- [NVIDIA NVSHMEM](https://developer.nvidia.com/nvshmem)
