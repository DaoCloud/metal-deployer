# Metal Deployer ISO 快速上手指南

本文面向第一次在本地环境使用本项目的人，目标是从 `git clone` 开始，完成自定义 Ubuntu 24.04 自动安装 ISO 的准备、配置、构建、测试和安装。

## 1. 环境要求

建议使用 Ubuntu 22.04/24.04 或其他 Debian/Ubuntu 系 Linux 作为构建机。

构建机需要：

- root/sudo 权限
- 网络访问能力，用于下载 Ubuntu ISO 和离线安装包
- 至少 50 GB 可用磁盘空间
- Ruby、curl
- 构建 ISO 时需要 `xorriso`、`mtools`、`dosfstools` 等工具，`build-iso/build.sh` 会自动尝试安装
- 如需 QEMU 本地验证，还需要 `qemu-system-x86_64`、`qemu-img`、`sshpass`、`openssh-client`
- 如需打包 Docker 测试镜像，还需要 Docker

## 2. 获取 ISO

有两种常见方式。

### 2.1 本地构建

适合需要修改 `manifest.yaml`、`user-data`、离线包或初始化脚本的场景。后续章节按本地构建展开。

### 2.2 下载预构建 ISO

- 发版后进入 GitHub Releases：<https://github.com/DaoCloud/metal-deployer/releases>
- Release Notes 会写明镜像拉取命令、内网 ISO 下载直链、同名 `.info.txt` 直链
- 默认 ISO 发布地址前缀：`http://10.64.40.200:5000/iso/GPU/`
- `Release ISO` workflow 会生成版本化文件名，格式类似 `metal-deployer-vX.Y.Z-ubuntu2404-YYYYMMDDHHMM-cuda13.iso`
- `.info.txt` 和 Release Notes 内嵌文本会列出 ISO 内全部 packages 与 versions
- 如果仓库还没有 Release Notes，或该次运行未上传到内网地址，可到 Actions 运行页面下载 artifact `custom-ubuntu-iso`

下载后的 ISO 可直接跳到 [第 8 节](#8-安装到物理服务器) 使用。

## 3. 获取代码

```bash
git clone <REPO_URL> metal-deployer
cd metal-deployer
```

如果只是构建 ISO，后续主要操作都在 `build-iso/` 目录中完成。

```bash
cd build-iso
```

## 4. 修改常用参数

主要配置文件是：

- `build-iso/manifest.yaml`：统一配置文件，包含通用 basic 配置和 CUDA profile 配置
- `build-iso/config/cloud-init/user-data`：Cloud-Init/autoinstall 模板，保留资源复制和首次启动入口
- `build-iso/config/ssh_authorized_keys`：可选，写入需要安装到目标机的 SSH 公钥

一般优先改 `manifest.yaml` 中的 `basic` 或 `cuda.profiles`，不要直接改安装流程脚本。两套 profile 的驱动、CUDA Toolkit、NCCL、CUDA samples 等版本应保持一致，避免安装或测试工具编译失败。

### 4.1 基础 ISO 来源

编辑统一 manifest，例如 `build-iso/manifest.yaml`：

```yaml
base_iso:
  source: "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso"
  local_path: "iso/ubuntu-24.04-base.iso"
```

如果现场有内网镜像，可以把 `source` 改为内网 URL。`prepare.sh` 默认会下载到 `build-iso/.ci-work/iso/ubuntu-24.04-base.iso`。

### 4.2 账号、主机名、密码

编辑所选 manifest 中的 `user_data.autoinstall.identity`：

```yaml
user_data:
  autoinstall:
    identity:
      hostname: myserver
      realname: admin
      username: admin
      password: "$6$..."
```

`password` 必须是 SHA-512 hash，不是明文。生成方式：

```bash
openssl passwd -6 admin
```

上面的示例会生成密码为 `admin` 的 hash。

### 4.3 磁盘安装目标和分区

默认配置会安装到最大磁盘，降低误装到 USB 启动盘的风险：

```yaml
user_data:
  autoinstall:
    storage:
      layout:
        name: direct
        match:
          size: largest
```

如需使用 LVM 全盘安装：

```yaml
user_data:
  autoinstall:
    storage:
      layout:
        name: lvm
        sizing-policy: all
        match:
          size: largest
```

如需固定安装到某块盘：

```yaml
user_data:
  autoinstall:
    storage:
      layout:
        name: direct
        match:
          path: /dev/sda
```

注意：安装会清空目标磁盘。固定 `path` 前必须确认目标机器上的盘符。

### 4.4 语言、时区、键盘

编辑：

```yaml
user_data:
  autoinstall:
    locale: en_US.UTF-8
    timezone: Etc/UTC
    keyboard:
      layout: us
      variant: ""
```

中国大陆环境常见时区：

```yaml
timezone: Asia/Shanghai
```

### 4.5 网络

默认使用 DHCP，匹配 `e*` 网卡：

```yaml
user_data:
  autoinstall:
    network:
      version: 2
      ethernets:
        default:
          match:
            name: "e*"
          dhcp4: true
          dhcp6: false
```

如果现场需要静态 IP，可以把 `network` 改成标准 netplan/autoinstall 网络配置。

### 4.6 SSH 登录

默认开启 SSH 服务和密码登录：

```yaml
user_data:
  autoinstall:
    ssh:
      install-server: true
      allow-pw: true
      authorized-keys: []
```

推荐把公钥写入 ISO 的配置目录，而不是直接写进 `manifest.yaml`：

```bash
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images
```

该命令会生成：

```text
build-iso/config/ssh_authorized_keys
```

目标机首次启动时，`configure_ssh.sh` 会把这些公钥写入 `admin` 和 `root` 的 `authorized_keys`。

### 4.7 离线包和安装阶段

离线包清单在所选 manifest 的 `packages` 中。`prepare.sh` 会根据 `url` 和 `filename` 下载到：

```text
build-iso/.ci-work/packages/
```

首次启动安装阶段在：

```yaml
user_data:
  installation:
    stages:
```

每个 stage 可以通过 `enabled: true|false` 控制是否执行。例如暂时关闭 GPU Burn 编译：

```yaml
- name: "gpu_burn_build"
  enabled: false
```

## 5. 准备构建资源

在 `build-iso/` 目录执行：

```bash
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images
```

这会准备：

- 基础 Ubuntu ISO：`build-iso/.ci-work/iso/ubuntu-24.04-base.iso`
- manifest 中列出的离线包：`build-iso/.ci-work/packages/`
- SSH 公钥：`build-iso/config/ssh_authorized_keys`

如果需要同时拉取并保存 Docker 镜像 tar 包，去掉 `--skip-images`：

```bash
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub
```

只预览下载和复制动作：

```bash
./prepare.sh --dry-run --skip-images
```

如果目标环境不能访问外网，也可以手动准备文件：

- 把 Ubuntu live server ISO 放到 `build-iso/.ci-work/iso/`
- 把 `.deb`、`.tar.gz`、`.tbz` 等离线包放到 `build-iso/.ci-work/packages/`
- 把 SSH 公钥写入 `build-iso/config/ssh_authorized_keys`

CI 或临时构建环境可以把下载缓存和中间产物统一放到一个目录，例如：

```bash
export ISO_WORK_DIR="$PWD/build-iso/.ci-work"
export MANIFEST_FILE="$PWD/build-iso/manifest.yaml"
export ISO_DIR="$ISO_WORK_DIR/iso"
export PACKAGE_DIR="$ISO_WORK_DIR/packages"
export IMAGE_DIR="$ISO_WORK_DIR/packages/images"
```

`prepare.sh` 会优先使用这些环境变量；本地未设置时仍使用默认目录。构建 CUDA 12.8 ISO 时设置 `CUDA_PROFILE=cuda12`。

## 6. 生成 ISO

在 `build-iso/` 目录执行：

```bash
sudo ./build.sh
```

构建过程会：

1. 解包基础 Ubuntu ISO
2. 注入 `packages/`、`scripts/`、`config/`
3. 根据所选 manifest 和 `user-data` 生成最终 `/nocloud/user-data`
4. 修改 GRUB，加入 autoinstall 和串口日志参数
5. 重新打包为可启动 ISO

默认生成结果：

```text
build-iso/.ci-work/custom-ubuntu.iso
```

中间工作目录：

```text
build-iso/.ci-work/build_workspace/
```

CI/release 构建可通过环境变量改写输出位置：

```bash
export BUILD_WORK_DIR="$PWD/build-iso/.ci-work/build_workspace"
export OUTPUT_ISO="$PWD/build-iso/.ci-work/custom-ubuntu.iso"
sudo --preserve-env=BUILD_WORK_DIR,OUTPUT_ISO,ISO_DIR,PACKAGE_DIR ./build-iso/build.sh
```

显式指定 profile 时：

```bash
CUDA_PROFILE=cuda12 ./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images
sudo CUDA_PROFILE=cuda12 ./build.sh
```

Release/CI 场景如果覆盖了 `OUTPUT_ISO`，产物文件名通常会变成：

```text
metal-deployer-vX.Y.Z-ubuntu2404-YYYYMMDDHHMM-cuda13.iso
```

## 7. 本地 QEMU 验证

在 `build-iso/` 目录执行：

```bash
./test.sh setup
```

脚本会创建测试磁盘并启动虚拟机自动安装。默认常用文件：

```text
build-iso/.ci-work/test_disk.qcow2
build-iso/.ci-work/qemu.log
build-iso/.ci-work/qemu.pid
```

如需把 QEMU 测试遗留物放到统一目录：

```bash
export TEST_WORK_DIR="$PWD/build-iso/.ci-work/qemu"
export ISO_FILE="$PWD/build-iso/.ci-work/custom-ubuntu.iso"
./build-iso/test.sh setup
./build-iso/test.sh clean
```

安装完成后登录虚拟机：

```bash
./test.sh login
```

查看安装日志：

```bash
cat /var/log/scripts.log
sudo cat /var/log/metal-deployer/install-summary.log
```

从虚拟机拉取文件：

```bash
./test.sh pull /var/log/scripts.log
```

清理测试环境：

```bash
./test.sh clean
```

## 8. 安装到物理服务器

有三种常见方式。

### 8.1 通过 BMC/IPMI 虚拟光驱安装

1. 上传或挂载 `build-iso/.ci-work/custom-ubuntu.iso`，或下载好的发布版 `.iso`，到服务器 BMC 的虚拟介质
2. 设置服务器从虚拟 CD/DVD 启动
3. 重启服务器
4. Ubuntu autoinstall 会自动分区、安装系统、复制资源并重启
5. 首次启动后会执行 `/opt/resource/scripts/init_system.sh`

### 8.2 写入 USB 启动盘

在 Linux/macOS 上确认 USB 设备名后执行：

```bash
sudo dd if=build-iso/.ci-work/custom-ubuntu.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

把 `/dev/sdX` 替换为真实 USB 设备，例如 `/dev/sdb`。不要写成分区路径如 `/dev/sdb1`。

### 8.3 PXE 或其他平台

如果现场平台支持 ISO 网络启动，可以直接使用：

```text
build-iso/.ci-work/custom-ubuntu.iso
```

内核启动参数已在 ISO 中写入：

```text
autoinstall ds=nocloud;s=/cdrom/nocloud/
```

## 9. 安装后的默认行为

安装完成后：

- 系统会自动重启
- 默认用户来自所选 manifest 的 `user_data.autoinstall.identity`
- ISO 内资源会复制到目标机 `/opt/resource/`
- 首次启动会执行 `/opt/resource/scripts/init_system.sh`
- 安装日志主要在：

```text
/var/log/scripts.log
/var/log/metal-deployer/
```

常用检查命令：

```bash
ls -la /opt/resource
cat /var/log/scripts.log
sudo cat /var/log/metal-deployer/install-summary.log
systemctl status ssh
```

GPU/RDMA 机器可继续检查：

```bash
nvidia-smi
dkms status
lspci | grep -i -E 'nvidia|mellanox'
```

## 10. 常见问题

### prepare.sh 下载失败

检查网络、代理和所选 manifest 中的 URL。如果使用内网镜像，把 `base_iso.source` 和包 URL 改为内网地址。

### build.sh 提示缺少工具

`build.sh` 会自动尝试安装依赖。如果构建机不能访问 apt 源，请先手动安装：

```bash
sudo apt-get update
sudo apt-get install -y xorriso mtools dosfstools ruby curl
```

### 安装到了错误磁盘

检查所选 manifest 中的：

```yaml
user_data:
  autoinstall:
    storage:
```

默认建议使用 `size: largest`。如果改成 `path: /dev/sda`，必须先确认目标机器实际盘符。

### 安装后无法 SSH

检查：

- `user_data.autoinstall.ssh.install-server` 是否为 `true`
- `user_data.autoinstall.ssh.allow-pw` 是否为 `true`
- 是否正确注入 `build-iso/config/ssh_authorized_keys`
- 目标机网络是否拿到 IP

### 首次启动安装失败

登录目标机后查看：

```bash
cat /var/log/scripts.log
sudo ls -la /var/log/metal-deployer/
sudo cat /var/log/metal-deployer/install-summary.log
```

通常原因是离线包缺失、包版本不匹配、安装阶段条件不满足，或目标机器硬件与启用的 stage 不匹配。
