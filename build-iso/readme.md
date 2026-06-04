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

- `manifest.yaml`：基础 ISO、离线包、Docker 镜像、CUDA profile、安装阶段配置
- `config/cloud-init/user-data`：autoinstall 模板
- `config/ssh_authorized_keys`：可选，注入到目标机的 SSH 公钥
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

如果要同时缓存 Docker 镜像 tar，去掉 `--skip-images`。

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
3. 根据 `manifest.yaml` 和 `config/cloud-init/user-data` 生成 `/nocloud/user-data`
4. 写入 autoinstall 启动参数
5. 重新打包为可启动 ISO

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
