# Metal Deployer

Metal Deployer 是一个用于裸金属服务器部署和性能测试的工具集，包含 ISO 镜像构建和 SuperBench GPU 集群性能测试两大核心功能。

## 项目概述

本项目提供以下核心能力：

| 功能模块 | 说明 | 路径 |
|---------|------|------|
| **ISO 构建** | 自定义 Ubuntu Server 24.04 自动化安装镜像 | [`build-iso/`](./build-iso/) |
| **GPU 性能测试** | 基于 SuperBench 的 GPU 集群一键性能测试 | [`superbenchmark/`](./superbenchmark/) |

---

## 一、ISO 镜像构建

位于 [`build-iso/`](./build-iso/) 目录，用于创建全自动化的 Ubuntu Server 24.04 定制安装镜像。

### 核心功能

- **全自动安装 (Autoinstall)**：基于 Cloud-Init 实现零交互安装
- **资源注入**：将离线 `.deb` 包和自定义脚本注入到 ISO
- **持久化搬运**：安装时自动将资源搬运到目标机器 `/opt/resource/`
- **初次启动自举**：系统首次启动时自动安装离线包并执行初始化脚本
- **双引导支持**：同时支持 Legacy BIOS (MBR) 和 UEFI 引导
- **自动化测试**：基于 QEMU 的测试脚本验证 ISO 安装

### 快速开始

```bash
cd build-iso/

# 1. 下载官方 ISO 到 iso/ 目录
wget -P iso/ https://releases.ubuntu.com/24.04/ubuntu-24.04-live-server-amd64.iso

# 2. 准备离线包到 packages/ 目录

# 3. 准备初始化脚本到 scripts/ 目录

# 4. 构建 ISO
sudo ./build.sh

# 5. 使用 QEMU 测试
./test.sh setup    # 启动虚拟机自动安装
./test.sh login    # SSH 登录虚拟机
./test.sh clean    # 清理测试环境
```

### 详细文档

- [ISO 构建完整文档](./build-iso/readme.md)

---

## 二、SuperBench GPU 性能测试

位于 [`superbenchmark/`](./superbenchmark/) 目录，提供基于 SuperBench 的 GPU 集群一键性能测试方案。

### 核心功能

- **一键测试**：通过 `run-tests.sh` 脚本自动执行单机和多机测试
- **容器化控制**：使用 sbcli 容器作为控制节点，无需本地安装依赖
- **统一报告**：自动合并多种测试结果到一个 Excel 文件
- **灵活配置**：支持 `single-node`、`multi-node`、`all` 三种测试模式

### 前置依赖

| 依赖项 | 要求 |
|--------|------|
| 控制节点 | Docker 已安装 |
| 被测节点 | Docker 已安装，已加载测试镜像 |
| SSH 连接 | 控制节点能通过 SSH 密钥连接所有被测节点 |

### 快速开始

```bash
# 1. 拉取控制镜像
docker pull ghcr.io/daocloud/metal-deployer/sbcli:latest

# 2. 准备 hosts 文件
cat > hosts << 'EOF'
[all]
node1 ansible_host=192.168.1.101
node2 ansible_host=192.168.1.102

[all:vars]
ansible_user=root
ansible_ssh_private_key_file=/root/.ssh/id_rsa
EOF

# 3. 启动控制容器
docker run -it \
  -v $(pwd)/hosts:/workspace/hosts \
  -v ~/.ssh:/root/.ssh:ro \
  -v $(pwd)/results:/workspace/outputs \
  --entrypoint /bin/bash \
  ghcr.io/daocloud/metal-deployer/sbcli:latest

# 4. 执行一键测试（容器内）
/workspace/scripts/run-tests.sh              # 默认：单机和多机测试
/workspace/scripts/run-tests.sh -t all     # 全量测试（约 8 小时）
```

### 测试结果

```
~/results/
└── {timestamp}/
    ├── deploy/                     # 部署日志
    ├── single-node/                # 单机测试结果
    ├── multi-node/                 # 多机测试结果
    └── reports/
        └── superbench-test-summary.xlsx  # 统一汇总报告
```

### 详细文档

- [SuperBench 测试完整文档](./superbenchmark/README.md)
- [详细配置说明](./docs/superbenchmark.md)

---

## 三、本地构建镜像

可以使用 Make 命令在本地构建 sbcli 镜像：

```bash
# 显示所有可用命令
make help

# 本地构建 sbcli 镜像
make build-sbcli

# 构建并推送 sbcli 镜像到仓库
make build-sbcli-push

# 测试本地构建的镜像
make test-sbcli

# 以交互模式测试镜像
make test-sbcli-interactive

# 清理本地镜像
make clean
```

### ISO 构建命令

```bash
# 构建 ISO 镜像（需要 root 权限）
make build-iso

# 使用 QEMU 测试 ISO
make test-iso

# 清理 ISO 构建环境
make clean-iso
```

## 四、镜像构建 CI/CD

本项目使用 GitHub Actions 自动构建 sbcli 镜像：

| 触发条件 | 行为 |
|---------|------|
| PR open/reopen | 构建镜像（不推送） |
| main 分支 push | 构建并推送 `latest` 标签 |
| release tag | 构建并推送版本标签 |

镜像地址：`ghcr.io/daocloud/metal-deployer/sbcli:latest`

---

## 四、项目结构

```
metal-deployer/
├── .github/
│   └── workflows/
│       └── build-sbcli-image.yml    # GitHub Actions 镜像构建
├── build-iso/                        # ISO 构建工具
│   ├── build.sh                      # ISO 构建脚本
│   ├── test.sh                       # QEMU 测试脚本
│   ├── user-data                     # Cloud-Init 配置
│   ├── iso/                          # 存放官方 ISO
│   ├── packages/                     # 离线 .deb 包
│   └── scripts/                      # 初始化脚本
├── superbenchmark/                   # GPU 性能测试
│   ├── README.md                     # 测试指南
│   └── image/                        # sbcli 镜像构建
│       ├── Dockerfile
│       ├── configs/                  # 测试配置文件
│       └── scripts/                  # 一键测试脚本
├── docs/                             # 详细文档
│   └── superbenchmark.md
├── Makefile                          # 本地构建工具
└── README.md                         # 本文档
```

---

## 五、参考链接

- [SuperBench 官方文档](https://microsoft.github.io/superbenchmark/)
- [Ubuntu Autoinstall](https://canonical-subiquity.readthedocs-hosted.com/)
- [Cloud-Init](https://cloudinit.readthedocs.io/)

---

## License

[LICENSE](./LICENSE)
