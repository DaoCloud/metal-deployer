
# Superbenchmark CLI 镜像

该目录新增了一个用于构建 `Superbenchmark CLI` 工具镜像的 Dockerfile，目标是提供一个可直接运行 `sb` 命令的轻量镜像，便于在控制节点或运维节点上执行 Superbenchmark 相关任务。

## 镜像内容

当前镜像定义位于 `image/Dockerfile`，基于 `python:3.11-slim` 构建，主要包含以下内容：

- **Superbenchmark 源码**
  - 构建时从 `https://github.com/microsoft/superbenchmark` 拉取源码。
  - 默认使用 `v0.12.0` 标签，可通过构建参数覆盖。

- **Python 运行环境**
  - 基于 Python 3.11。
  - 通过 `python -m pip install .` 安装 Superbenchmark CLI。

- **系统工具**
  - `ca-certificates`
  - `git`
  - `openssh-client`
  - `rsync`
  - `sshpass`
  - `vim-tiny`

- **SSH 默认配置**
  - 镜像内写入 `/etc/ssh/ssh_config`。
  - 默认关闭严格主机校验，避免批量执行时因 host key 交互导致失败。

## 功能说明

该镜像主要用于提供一个开箱即用的 `sb` 命令执行环境，适合以下场景：

- **作为控制节点 CLI 镜像**
  - 在容器中直接运行 `sb version`、`sb deploy`、`sb run` 等命令。

- **用于远程批量执行**
  - 镜像内已包含 SSH 客户端、`rsync`、`sshpass` 等工具，便于配合 inventory 或自动化流程下发任务。

- **用于集群环境基线测试与性能测试编排**
  - 适合作为 Superbenchmark 控制端容器，而不是节点侧的业务运行镜像。

## Patch 说明

当前镜像在构建过程中会应用补丁文件：`image/patches/control-node-use-inventory-hosts.patch`。

该补丁主要包含两类修复：

- **兼容新版 pandas**
  - 修复 `result_summary.py` 中 `DataFrame.drop()` 的调用方式，避免新版 pandas 对 `columns=` 和 `axis=` 混用时报错。
  - 修复 `to_excel()` 调用方式，改为显式使用 `sheet_name=` 等关键字参数，以兼容新版 pandas 的关键字参数约束。

- **控制节点使用 inventory 主机名/地址**
  - 修改 `check_env.yaml` 中节点列表来源，从 `ansible_hostname` 改为直接使用 inventory 中的主机条目。
  - `NODE_RANK` 的计算也从 `ansible_hostname` 切换为 `inventory_hostname`。
  - 这样做的目的是让 MPI/远程执行使用与 Ansible inventory 一致的可达地址，避免节点之间无法解析 `ansible_hostname` 时导致失败。

## 构建说明

在 `superbenchmark` 目录下执行：

```bash
docker build -t superbenchmark-cli:latest -f image/Dockerfile image
```

## 可选构建参数

可以通过如下参数覆盖上游仓库地址和版本：

```bash
docker build \
  -t superbenchmark-cli:latest \
  -f image/Dockerfile image \
  --build-arg SUPERBENCH_REPO_URL=https://github.com/microsoft/superbenchmark \
  --build-arg SUPERBENCH_TAG=v0.12.0
```

## 构建结果验证

Dockerfile 中已经在构建阶段执行：

```bash
sb version
```

用于验证 CLI 安装是否成功。

镜像构建完成后，也可以手动验证：

```bash
docker run --rm superbenchmark-cli:latest sb version
```

## 运行方式说明

该镜像默认入口为：

```bash
sleep infinity
```

因此可以作为常驻工具容器使用，例如：

```bash
docker run -it --rm superbenchmark-cli:latest
```

或者覆盖入口直接执行命令：

```bash
docker run --rm --entrypoint sb superbenchmark-cli:latest version
```

## 目录结构

```text
superbenchmark/
├── README.md
└── image/
    ├── Dockerfile
    └── patches/
        └── control-node-use-inventory-hosts.patch
```