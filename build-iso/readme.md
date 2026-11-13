# Metal Deployer ISO 构建与使用说明

本文档说明 `build-iso/` 目录当前真实工作流：如何准备输入、构建 ISO、做本地验证、去哪里下载预构建 ISO、以及如何把 ISO 用到物理机安装。

## 1. 构建产物与下载入口

### 本地构建产物

默认输出文件：

```text
build-iso/.ci-work/custom-ubuntu.iso
```

默认测试产物：

```text
build-iso/.ci-work/test_disk.qcow2
build-iso/.ci-work/qemu.log
build-iso/.ci-work/qemu.pid
```

### 预构建 ISO 下载

- 发版后进入 GitHub Releases：<https://github.com/DaoCloud/metal-deployer/releases>
- Release Notes 会写明内网 ISO 下载直链；默认发布地址前缀是 `http://10.64.40.200:5000/iso/GPU/`
- 文件名格式类似 `metal-deployer-vX.Y.Z-ubuntu2404-YYYYMMDDHHMM-cuda13.iso`
- 同目录会有同名 `.info.txt`
- `.info.txt` 和 Release Notes 内嵌文本都会列出 ISO 内全部 packages 与 versions
- 如果某次发版没有上传到内网地址，可在 GitHub Actions 运行页面下载 artifact `custom-ubuntu-iso`

## 2. 本地构建

### 关键文件

- `manifests/manifest.yaml`：基础 ISO、离线包、Docker 镜像、CUDA profile、安装阶段配置（x86_64）
- `manifests/manifest-gb300.yaml`：NVIDIA GB300 系列（Grace Blackwell，arm64）专用 manifest，版本对齐 [DGX OS 7 Release Notes](https://docs.nvidia.com/dgx/dgx-os-7-user-guide/release_notes.html)；构建工具链的 arm64 支持仍在进行中，见文件头部说明
- `config/cloud-init/user-data`：autoinstall 模板
- `config/ssh_authorized_keys`：可选，注入到目标机的 SSH 公钥（一行一个）。首启第一个阶段 `ssh_keys` 写入 `admin` 和 `root` 的 `authorized_keys`，并写入 `/etc/ssh/sshd_config.d/10-metal-deployer.conf`（`PermitRootLogin prohibit-password`）：root 只能用密钥登录，不能用密码登录
- `scripts/`：首次启动执行的初始化脚本

### 准备输入

```bash
cd build-iso
./prepare.sh --ssh-public-key ~/.ssh/id_ed25519.pub --skip-images
```

默认行为：

1. 下载基础 Ubuntu ISO 到 `build-iso/.ci-work/iso/`
2. 下载 manifest 中声明的离线包到 `build-iso/.ci-work/packages/`
3. 生成 `build-iso/config/ssh_authorized_keys`

如果要同时缓存 Docker 镜像 tar，去掉 `--skip-images`。manifest 的 `docker_images` 条目支持以下可选字段：

| 字段 | 作用 |
|---|---|
| `platform` | 按指定架构拉取并校验，例如 GB300 用 `linux/arm64`；`docker save` 只保留该架构 |
| `digest` | 按 digest 拉取以固定内容，再打回 `image` 的 tag，节点导入后名字不变 |
| `download: false` | 不下载、不打进 ISO |

GB300 manifest 中的 SuperBench 镜像直接从 Docker Hub 拉取（`docker.io/superbench/main`，不在 daocloud 镜像站白名单内），构建机需能访问 Docker Hub。

只检查远端 URL 与动作，不实际下载：

```bash
./prepare.sh --dry-run --skip-images
```

### 构建 ISO

```bash
sudo ./build.sh
```

如需显式指定 CUDA profile：

```bash
sudo CUDA_PROFILE=cuda12 ./build.sh
sudo CUDA_PROFILE=cuda13 ./build.sh
```

`build.sh` 会：

1. 从 `build-iso/.ci-work/iso/` 读取基础 ISO
2. 注入离线包、脚本、配置文件
3. 根据 `manifests/manifest.yaml` 和 `config/cloud-init/user-data` 生成 `/nocloud/user-data`
4. 写入 autoinstall 启动参数
5. 重新打包为可启动 ISO

### 构建 GB300（arm64）ISO

在 amd64 主机上即可构建，建议使用独立工作目录，避免和 x86 构建的缓存混用：

```bash
cd <repo-root>
export CI_WORK_DIR=$PWD/build-iso/.ci-work/gb300 CACHE_BASE_DIR=$PWD/build-iso/.ci-work/gb300-cache
make prepare-iso ISO_MANIFEST=build-iso/manifests/manifest-gb300.yaml CUDA_PROFILE=cuda13-gb300
sudo -E make build-iso ISO_MANIFEST=build-iso/manifests/manifest-gb300.yaml CUDA_PROFILE=cuda13-gb300
```

- `prepare.sh` 发现 manifest 目标架构（`platform.arch: arm64`）与主机不同时，会在 `$CI_WORK_DIR/apt-arm64` 建立独立的 apt 环境（`ports.ubuntu.com`，可用 `PORTS_MIRROR` 指定内网镜像），并用基础 ISO 里已安装包的状态作为起点，让 apt 求解一套**能同时装上**的离线包：apt 包组 + 平台内核 + 驱动包列表 + manifest 直接下载的 deb + CUDA toolkit。只下载求解结果中缺的包，缓存放在 `base-packages-arm64`；不在结果里的旧包会被清理。求解失败时会打印冲突并退出，不会生成首启装不上的 ISO。
- `build.sh` 对 arm64 只生成 UEFI 引导：读取原版 ISO 的引导布局（`xorriso -report_el_torito as_mkisofs`）原样重放，串口参数取 manifest 的 `SERIAL_CONSOLE`（`ttyAMA0`）。
- 平台内核在**安装阶段**装好：`late-commands` 在目标系统里执行 `scripts/install_target_kernel.sh`，从离线包安装 `KERNEL_PACKAGE`（linux-nvidia-64k-hwe-24.04）。安装器重启后第一次开机就是 7.0.0-1024-nvidia-64k，首启脚本一次跑完，不需要再重启。日志在 `/var/log/metal-deployer-target-kernel.log`。
- 系统盘用 `storage.layout.match: size: smallest`：GB300 compute tray 是 1 块 M.2 NVMe 系统盘（1.92/2 TB）加 4–8 块 E1.S NVMe 数据盘（3.84/4 TB），`largest` 会把系统装到 E1.S 上。安装开始时 `early-commands` 运行 `scripts/select_root_disk.sh`，推算会选中哪块盘；选中 USB/可移动盘，或最小容量有多块不同型号的盘（如 M.2 与 E1.S 同容量）时，在分区前中止安装。日志在装好的系统 `/var/log/installer/metal-deployer-root-disk.log`。不重做 ISO 也能改选盘规则，见下方“网络启动”的内核参数。
- 卷标为 `METAL-<平台>-<架构>`（GB300 是 `METAL-GB300-ARM64`），可用 `ISO_VOLUME_ID` 覆盖；x86 保持 `CUSTOM_SERVER`。
- 参考耗时（2026-11-05，本地，首次）：prepare 约 96 分钟（含 3.6 GB CUDA local repo），build 约 5 分钟，ISO 约 12 GB（不含 Docker 镜像）。

GB300 manifest 的宿主机 DCGM 升级为 4.7.1（包版本 `1:4.7.1-2`），包含核心服务、同版本客户端、CUDA 13 诊断插件和专有诊断插件的完整依赖，下载约 982 MiB；尚未在 GB300 实机验证。另含 multinode 包（`datacenter-gpu-manager-4-multinode`、`-multinode-cuda13`，约 5 MiB），提供 `dcgmi mndiag` 的 `mnnvbandwidth`、`nvloom`、`mnubergemm`，用于 rack 级 NVLink Fabric 测试（见 [验收计划 S7b](../docs/gb300-acceptance-plan.md)），依赖首启安装的 `openmpi-bin`。DCGM 私有计算库不会替换系统 CUDA 13.0.3、NCCL 2.31.2 或 SuperBench 镜像内的库，GPU 驱动保持 580.178.04。

#### GB300 激进版（备用 ISO）：`cuda13-gb300-aggressive`

默认 profile `cuda13-gb300` 对齐 DGX OS 7.6.0。如果某个性能特性开不了，或者遇到已在新版本修复的 bug，可以改用 `cuda13-gb300-aggressive` 打一个备用 ISO。安装流程不变：GPU 驱动仍用 Canonical 签名的预编译模块，不走 DKMS，开着 Secure Boot 也能装。只是版本更新：

| 组件 | `cuda13-gb300` | `cuda13-gb300-aggressive` |
|---|---|---|
| 内核 | 7.0.0-1024-nvidia-64k | 7.0.0-1025-nvidia-64k |
| GPU 驱动 | 580.178.04（LTSB） | 615.71.09（New Feature Branch） |
| CUDA | 13.0.3 | 13.4.3 |
| NCCL / cuDNN | 2.31.2 / 9.24.0 | 2.32.3 / 9.28.1 |
| DCGM | 4.7.1 | 4.8.0 |
| DOCA-OFED | 3.2.3 LTS | 3.5.0（非 LTS） |
| Container Toolkit | 1.20.0 | 1.20.1 |

```bash
export CI_WORK_DIR=$PWD/build-iso/.ci-work/gb300-aggressive CACHE_BASE_DIR=$PWD/build-iso/.ci-work/gb300-cache
make prepare-iso ISO_MANIFEST=build-iso/manifests/manifest-gb300.yaml CUDA_PROFILE=cuda13-gb300-aggressive
sudo -E make build-iso ISO_MANIFEST=build-iso/manifests/manifest-gb300.yaml CUDA_PROFILE=cuda13-gb300-aggressive
```

- R615 是 New Feature Branch，支持周期短，只当备用，不作为默认。
- 没有用 R620（2026-11-04 发布的新 LTSB）+ CUDA 13.5：Ubuntu 还没有发布 R620 的 nvidia-64k 签名模块，用它就得走 DKMS 和 MOK。等 Canonical 发布后，改这个 profile 的版本号即可。
- profile 可以覆盖 basic 中的包：profile 里同名的条目会原位替换 basic 条目；`exclude_packages` 按名字删除 basic 条目，例如 1024 内核的 ABI 包。
- 生成的 `/.metal-deployer/release.json` 里 `cuda_profile` 字段写明是哪个 profile；卷标两者相同，需要区分时用 `ISO_VOLUME_ID` 覆盖。

旧 DCGM 诊断客户端不能与新版 hostengine 混用。自定义脚本需将 `dcgmi diag --debugLogFile` 改为 `--log-filename`，参数仅接受文件名，不接受目录路径。容器若连接宿主机 hostengine 执行诊断，也需同步匹配客户端；独立运行容器内 DCGM 不会自动使用宿主机版本。

在 amd64 主机上用 QEMU 验证 UEFI 启动（需 `qemu-system-arm qemu-efi-aarch64`；纯软件模拟，安装很慢）：

```bash
cp /usr/share/AAVMF/AAVMF_VARS.fd vars.fd
qemu-img create -f qcow2 disk.qcow2 200G
qemu-system-aarch64 -M virt -cpu max -smp 16 -m 16G \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd \
  -drive if=pflash,format=raw,file=vars.fd \
  -drive file=custom-ubuntu.iso,format=raw,if=none,id=cd,readonly=on \
  -device virtio-scsi-pci -device scsi-cd,drive=cd,bootindex=1 \
  -drive file=disk.qcow2,format=qcow2,if=none,id=hd -device virtio-blk-pci,drive=hd,bootindex=2 \
  -netdev user,id=n0,hostfwd=tcp::12222-:22 -device virtio-net-pci,netdev=n0 \
  -device virtio-gpu-pci -display none -vnc 127.0.0.1:77 \
  -serial file:serial.log -monitor unix:monitor.sock,server,nowait
```

内核参数最后的 `console=tty0` 是主控制台，systemd 之后的输出不在串口上；通过 VNC 或 monitor 的 `screendump` 查看安装界面。

### 网络启动（NICo / iPXE）

ISO 同时支持 BMC 挂载启动和网络启动，交付物仍是一个 ISO。网络启动时 ISO 自带的 GRUB 和 `/nocloud` 不会被用到，相关约定写在 ISO 里：

| ISO 内路径 | 用途 |
|---|---|
| `casper/vmlinuz`、`casper/initrd` | iPXE 加载的内核和 initrd，对应 NICo `ubuntu-autoinstall` 模板的 `kernel`、`initrd` |
| `autoinstall.yaml` | 与 `/nocloud/user-data` 内容相同。网络启动时 NICo 下发的 cloud-config 没有 `autoinstall` 段，安装程序就使用这份配置 |
| `.metal-deployer/release.json` | 平台、架构、版本、目标内核、驱动分支、上述文件路径和推荐的 NICo 模板，供解开 ISO 的发布程序读取 |

NICo 内置 `ubuntu-autoinstall` 模板的内核参数为 `url=<ISO 地址> autoinstall ds=nocloud-net;s=<元数据地址>`，安装程序会把整个 ISO 下载进内存再挂成 `/cdrom`，节点可用内存需大于 ISO 体积。

- **不用 phone-home**：NICo 的 `userData` 填 `#cloud-config` 即可（不要写 `autoinstall` 段），安装配置来自 ISO 的 `autoinstall.yaml`。
- **用 phone-home**：把 `nocloud/user-data` 的内容填进 `userData`。NICo 会把回报配置插到 `autoinstall.user-data` 下，由装好的系统回报。

装完重启后 NICo 返回"从本地硬盘启动"，直接进入已安装系统，不会重装。

GB300 ISO 可以在 NICo Operating System 定义的内核参数里追加以下参数，调整系统盘选择，不需要重做 ISO：

| 参数 | 作用 |
|---|---|
| `md.root_disk=smallest` / `largest` | 按容量选盘（GB300 默认 `smallest`） |
| `md.root_disk=model:<通配>` | 按 udev `ID_MODEL` 选盘，如 `model:SAMSUNG_MZ1L2*`（型号可从 `nico-admin-cli machine hardware-info show` 查到） |
| `md.root_disk=id_path:<通配>` | 按插槽路径 udev `ID_PATH` 选盘，如 `id_path:pci-0008:01:00.0-*` |
| `md.root_disk=serial:<通配>` / `path:<通配>` | 按序列号或设备路径选盘 |
| `md.root_disk_check=0` | 关闭检查，只记录日志（不推荐） |

参数值不能含空格；udev 的 `ID_MODEL` 已把空格换成下划线。

## 3. 本地验证

启动 QEMU 自动安装：

```bash
./test.sh setup
```

登录测试虚拟机：

```bash
./test.sh login
```

拉取日志或文件：

```bash
./test.sh pull /var/log/scripts.log
```

清理测试环境：

```bash
./test.sh clean
```

常看日志：

```text
/var/log/scripts.log
/var/log/metal-deployer/
```

## 4. 如何使用 ISO

### 方式 1：BMC/IPMI/iDRAC/iLO 虚拟光驱

1. 把 `build-iso/.ci-work/custom-ubuntu.iso` 或下载好的发布版 ISO 上传/挂载到虚拟介质
2. 设置服务器从虚拟光驱启动
3. 重启服务器
4. 安装器自动分区、安装系统、复制资源并重启

### 方式 2：写入 USB 启动盘

```bash
sudo dd if=build-iso/.ci-work/custom-ubuntu.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

`/dev/sdX` 必须替换为真实 U 盘设备，不是分区路径。

### 方式 3：平台支持 ISO 网络启动

直接使用生成或下载的 `.iso` 文件即可。ISO 内已写入：

```text
autoinstall ds=nocloud;s=/cdrom/nocloud/
```

## 5. 当前推荐阅读顺序

1. 仓库总览：[`../README.md`](../README.md)
2. 快速上手：[`../guide.md`](../guide.md)
3. 本文档：聚焦 `build-iso/` 目录当前命令、产物路径、下载入口与使用方式
