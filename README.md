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

# 1. 下载基础 ISO、离线包，并注入 SSH 公钥
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images

# 2. 构建 ISO
sudo ./build.sh

# 3. 使用 QEMU 做本地冒烟测试
./test.sh setup
./test.sh login
./test.sh clean
```

默认产物位置：`build-iso/.ci-work/custom-ubuntu.iso`

### 下载发版 ISO

- 发版后先看 GitHub Releases：<https://github.com/DaoCloud/metal-deployer/releases>
- Release Notes 会写明内网 ISO 下载直链和同名 `.info.txt` 直链
- 默认发布地址前缀：`http://10.64.40.200:5000/iso/GPU/`
- 如果某次运行没有推送到内网地址，可去 Actions 运行页面下载 artifact `custom-ubuntu-iso`

`.info.txt` 会列出该 ISO 内全部 basic/profile packages 与对应版本；相同内容也会写进 Release Notes

### 如何使用 ISO

- 物理机：把 `build-iso/.ci-work/custom-ubuntu.iso` 或下载好的发布版 ISO 挂载到 BMC/IPMI/iDRAC/iLO 虚拟光驱后重启安装
- U 盘：`sudo dd if=build-iso/.ci-work/custom-ubuntu.iso of=/dev/sdX bs=4M status=progress oflag=sync`
- 支持平台 ISO 网络启动时，也可直接使用生成或下载的 `.iso`

### 详细文档

- [ISO 构建完整文档](./build-iso/readme.md)
- [ISO 快速上手指南](./guide.md)

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

### ISO 常用命令

```bash
# 准备基础 ISO、离线包、公钥
cd build-iso && ./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images

# 构建 ISO（需要 root 权限）
cd build-iso && sudo ./build.sh

# 使用 QEMU 测试 ISO
cd build-iso && ./test.sh setup
cd build-iso && ./test.sh login
cd build-iso && ./test.sh clean
```

## 四、镜像构建 CI/CD

本项目使用 GitHub Actions 自动构建镜像和 ISO：

| 触发条件 | 行为 |
|---------|------|
| PR open/reopen | 构建镜像（不推送） |
| main 分支 push | 构建并推送 `latest` 标签 |
| release tag | 构建并推送版本标签 |
| 手动触发 `Release ISO` | 构建版本化 ISO，上传 artifact，推送到内网 `destination_url`，并把下载方式与包版本写入 Release Notes |

### 发版产物下载

| 产物 | 发布位置 | 下载方式 |
| --- | --- | --- |
| `sbcli` | GHCR | `docker pull ghcr.io/daocloud/metal-deployer/sbcli:<version>` |
| `gpu-rdma-bench` | GHCR | `docker pull ghcr.io/daocloud/metal-deployer/gpu-rdma-bench:<version>` |
| ISO | 内网文件地址 | 去 Releases 页面查看 Release Notes 中的 ISO 直链，默认前缀 `http://10.64.40.200:5000/iso/GPU/` |
| ISO 包清单 | 同名 `.info.txt` | 下载 Release Notes 里的 `.info.txt`，其中列出 ISO 内全部 packages 与 versions |

发版时 Release Notes 会自动写入：

1. GHCR 镜像拉取地址
2. ISO 内网下载地址
3. ISO 同名 `.info.txt` 下载地址
4. ISO 内 packages/version 清单

---

## 五、项目结构

```
metal-deployer/
├── .github/
│   └── workflows/
│       └── build-sbcli-image.yml    # GitHub Actions 镜像构建
├── build-iso/                        # ISO 构建工具
│   ├── build.sh                      # ISO 构建脚本
│   ├── test.sh                       # QEMU 测试脚本
│   ├── config/                       # 配置文件
│   │   └── cloud-init/user-data      # Cloud-Init 模板
│   ├── manifest.yaml                 # ISO 构建清单
│   ├── .ci-work/                     # 默认下载缓存、构建产物、QEMU 测试产物
│   ├── packages/                     # 离线 .deb 包与镜像 tar 缓存
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

## 六、参考链接

- [SuperBench 官方文档](https://microsoft.github.io/superbenchmark/)
- [Ubuntu Autoinstall](https://canonical-subiquity.readthedocs-hosted.com/)
- [Cloud-Init](https://cloudinit.readthedocs.io/)

---

## License

[LICENSE](./LICENSE)
