# Ubuntu Server ISO 重构与自动化测试方案文档

## 1. 方案用途与原理

本方案旨在创建一个**全自动化的 Ubuntu Server 24.04 定制安装镜像 (ISO)**。
它不仅仅是一个重打包工具，还包含了一整套从**构建**到**自动化验证**的完整闭环流程。

### 核心功能
*   **全自动安装 (Autoinstall)**: 基于 `config/cloud-init/user-data` (Cloud-Init) 实现零交互安装，自动分区、创建用户。
*   **资源注入**: 将离线 `.deb` 包和自定义脚本注入到 ISO 中。
*   **持久化搬运**: 安装过程中自动将 ISO 内的资源 (`packages` 和 `scripts`) 搬运到目标机器的 `/opt/resource/` 目录。
*   **初次启动自举**: 系统第一次启动 (First Boot) 时，自动批量安装离线包并执行初始化脚本。
*   **双引导支持**: 生成的 ISO 同时支持 **Legacy BIOS (MBR)** 和 **UEFI** 引导模式。
*   **自动化测试**: 提供基于 QEMU 的测试脚本，一键验证 ISO 是否能成功安装并启动 SSH 服务。

### 工作原理
1.  **解包**: 使用 `xorriso` 解压官方 ISO。
2.  **注入**: 将本地 `packages/` 和 `scripts/` 目录复制到解压后的 ISO 内容中；将 `config/cloud-init/user-data` 放入 `/nocloud/` 目录供安装器读取。
3.  **配置**: 修改 Grub 配置以支持串口日志 (方便 QEMU 调试) 并缩短等待时间。
4.  **引导修复**: 重新生成 EFI 启动镜像 (`efi.img`) 并提取 MBR 模板，确保新 ISO 可启动。
5.  **重打包**: 使用 `xorriso` 生成新的混合引导 ISO。
6.  **安装流程**:
    *   安装器读取 `user-data` 配置。
    *   `late-commands` 阶段：将光盘里的资源拷贝到硬盘 (Target OS)。
    *   `runcmd` 阶段 (First Boot)：进入系统后执行 `apt-get install` 和自定义脚本。

---

## 2. 目录结构

```text
rebuildIso/
├── build.sh            # [核心] ISO 构建脚本
├── test.sh             # [工具] QEMU 自动化验证脚本
├── config/
│   ├── cloud-init/
│   │   └── user-data   # [配置] Cloud-Init 自动化安装配置
│   ├── rdma_modules.conf
│   └── ssh_authorized_keys.example
├── iso/                # [输入] 存放官方 Ubuntu Server ISO (需自行下载)
├── packages/           # [资源] 存放需离线安装的 .deb 包
└── scripts/            # [资源] 存放系统初始化脚本 (如 init_system.sh)
    └── init_system.sh  # [资源] (其他安装命令，可以放在此处)
```

---

## 3. 使用步骤

### 第一步：准备环境与资源

1.  **执行构建前准备脚本**:
    `prepare.sh` 会读取所选 manifest，下载基础 ISO、离线包，并可选预加载 Docker 镜像 tar。
    默认 profile 是 CUDA 13；也可以显式选择 CUDA 12.8 或 CUDA 13.2：

```bash
cd build-iso
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub
CUDA_PROFILE=cuda12 MANIFEST_FILE="$PWD/manifest.yaml" ./prepare.sh --skip-images
```

    如暂时不想拉取 Docker 镜像：

```bash
./prepare.sh --skip-images
```

    校验远程下载地址并预览下载动作：

```bash
./prepare.sh --dry-run --skip-images
```

2.  **下载官方 ISO（手动方式）**:
    将 Ubuntu 24.04 Server ISO 文件 (如 `ubuntu-24.04.3-live-server-amd64.iso`) 放入 `iso/` 目录。

3.  **准备离线包（手动方式）**:
    将需要预装的 `.deb` 文件放入 `packages/` 目录。

4.  **准备脚本**:
    将初始化脚本放入 `scripts/` 目录下，通过 `init_system.sh` 作为调用入口

### 第二步：构建 ISO
以 Root 权限运行 `build.sh`：

```bash
sudo ./build.sh
```

如果准备阶段选择了 CUDA 12.8，构建阶段也要使用同一个 CUDA profile：

```bash
sudo CUDA_PROFILE=cuda12 MANIFEST_FILE="$PWD/manifest.yaml" ./build.sh
```

**脚本行为**:
*   自动检查并通过 `apt-get` 安装 `xorriso`, `sed`, `mtools`, `dosfstools` 等依赖。
*   在 `build_workspace` 目录清理并提取 ISO。
*   注入资源和 `config/cloud-init/user-data`。
*   生成最终文件：`custom-server-24.04.iso`。

### 第三步：验证 ISO (自动化测试)

构建完成后，使用 `test.sh` 在本地启动一个 QEMU 虚拟机进行模拟安装测试。
新版测试脚本支持分步调试，提供了 `setup`, `login`, `clean` 三个子命令。

#### 1. 启动全自动测试
```bash
# 启动虚拟机 -> 自动安装 -> 等待 SSH -> 执行验证
./test.sh setup
```

**测试流程**:
1.  创建一个 10GB 的临时虚拟磁盘文件 (`test_disk.qcow2`)。
2.  启动 QEMU (无图形界面，日志输出到 `qemu.log`，串口日志直接输出到屏幕)。
3.  **自动轮询**: 脚本会循环尝试 SSH 连接。
4.  **保留现场**: 即使测试通过，**虚拟机也会继续运行**，方便你手动登录检查。

#### 2. 手动登录 (调试用)
在 `setup` 成功（或正在运行中）时，可以使用此命令快捷登录到虚拟机内部，无需手动输入 IP 和端口。

```bash
# ssh 登录 虚拟机
./test.sh login
    # 设置 root 密码
    sudo passwd root

    # check installation log
    cat /var/log/scripts.log
    cat /var/log/install-deb.log

# 从虚拟机上拉取目录或者文件到当前目录下
./test.sh pull <VM_Path>
```

#### 3. 清理环境 (结束测试)
测试完毕后，务必执行清理命令来停止 QEMU 进程并删除临时磁盘文件。

```bash
./test.sh clean
```


---

## 4. 自定义配置指南

### 修改用户名/密码
编辑 `config/cloud-init/user-data` 文件中的 `identity` 字段：
*   **username**: 默认为 `admin`。
*   **password**: 默认为 `admin` (注意：文件中存储的是 SHA-512 哈希值)。
    *   生成新哈希的方法: `mkpasswd -m sha-512` (需安装 `whois` 软件包)。

### 修改安装后的行为
编辑 `config/cloud-init/user-data` 文件中的 `runcmd` 部分：
*   目前配置为自动安装 `/opt/resource/packages/*.deb`。
*   目前配置为执行 `/opt/resource/scripts/*.sh`。
*   你可以在此添加任意 Shell 命令。

### 调整测试参数
编辑 `test.sh` 开头的配置区域：
*   `TIMEOUT`: 安装最大等待时间 (默认 1200秒)。
*   `SSH_PORT`: 映射端口。

---

## 5. 常见问题排查

| 问题现象 | 可能原因 | 解决方案 |
| :--- | :--- | :--- |
| **构建时报错 `xorriso: not found`** | 缺少依赖工具 | 确保联网，脚本会自动尝试 `apt-get install`，或手动安装依赖。 |
| **QEMU 测试时一直无法 SSH** | 安装过程卡住或网络问题 | 去掉 `test.sh` 中的 `-nographic` 参数，查看图形界面报错；检查内存是否分配不足 (默认 4G)。 |
| **安装后 `/opt/resource` 为空** | `late-commands` 执行失败 | 检查 ISO 根目录结构，确认资源是否正确复制到了 `/cdrom/resource`。 |
| **卡在 "End Kernel Panic"** | EFI 过旧或生成失败 | 确认 `build.sh` 中 `efi.img` 的生成逻辑是否正确执行。 |
