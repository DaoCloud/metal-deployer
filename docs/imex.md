# NVLink 跨节点通信（IMEX）— GB300 NVL72

本文说明 ISO 对 NVIDIA IMEX（Internode Memory Exchange）的支持范围、装机后如何配置启动、以及如何一键验证 NVLink 跨节点通信。背景原理与完整排障见飞书文档《NVLink 跨节点测试（IMEX）》。

## 1. 这是什么

GB300 NVL72 一个机柜 = 18 个计算托盘（72 GPU）+ 9 个 NVSwitch 托盘，组成一个 NVLink 域。GPU 可直接读写**其他托盘**的显存（Multi-Node NVLink, MNNVL）。

- 交换机侧：NMX-C（GFM / NVLSM）跑在 Leader 交换托盘上，出厂固件，负责拓扑发现与路由。节点侧 **不需要也不应启用** `nvidia-fabricmanager`。
- 节点侧：`nvidia-imex` 守护进程在托盘之间交换显存句柄（gRPC，默认 TCP 50000，走管理网），CUDA 才能 `cuMemImportFromShareableHandle(CU_MEM_HANDLE_TYPE_FABRIC)` 远端显存；NCCL ≥ 2.22 据此自动启用 MNNVL。数据面走 NVLink，IMEX 只是控制面。

## 2. ISO 做了什么 / 没做什么

| 项 | 状态 | 位置 |
|---|---|---|
| 安装 `nvidia-imex-580`、`nvidia-modprobe`、R580 open 驱动、CUDA 13、NCCL 2.31 | ✅ 首启安装 | [manifest-gb300.yaml](../build-iso/manifests/manifest-gb300.yaml) `cuda.profiles.cuda13-gb300`（备用 `cuda13-gb300-aggressive` 为 `nvidia-imex-615` + R615 + CUDA 13.4 + NCCL 2.32） |
| `options nvidia NVreg_CreateImexChannel0=1`（`/dev/nvidia-caps-imex-channels/channel0`） | ✅ | [nvidia_setup.sh](../build-iso/scripts/nvidia_setup.sh) → `/etc/modprobe.d/nvidia-imex.conf` |
| systemd drop-in `ConditionPathExists=/etc/nvidia-imex/nodes_config.cfg` | ✅ | [config/imex/10-wait-config.conf](../build-iso/config/imex/10-wait-config.conf) |
| `systemctl enable nvidia-imex`（无节点列表时静默不启动） | ✅ | nvidia_setup.sh |
| GB300 下不 enable `nvidia-fabricmanager` | ✅ | nvidia_setup.sh（`PLATFORM=gb300`，可用 `SKIP_FABRIC_MANAGER=0/1` 覆盖） |
| 运维脚本 `imex-configure.sh` / `imex-check.sh` / `imex-bench.sh` | ✅ | `/opt/resource/scripts/` |
| 测试工具 nccl-tests、nvbandwidth（`-DMULTINODE=1`）、`imex_fabric_ipc_test` | ✅ 首启编译 | `/opt/gpu-rdma-test-tools/bin/` |
| QEMU 打包自检 `imex-check.sh --smoke`（`nvidia-imex -nogpu` 回环域） | ✅ | [test.sh](../build-iso/test.sh) |
| **启动 `nvidia-imex`** | ❌ 装机阶段不做 | 需要柜内全部 18 节点管理 IP，装机时未知 |

## 3. 装机后配置（Day-2）

```bash
# 跳板机：每柜一个列表，18 行管理网 IP，分发到各节点
cat > rack1.list <<'LIST'
10.20.1.11
...
10.20.1.28
LIST
for ip in $(cat rack1.list); do scp rack1.list root@$ip:/root/; done

# 每个节点执行一次（可 pdsh 并行）
pdsh -w ^rack1.list "/opt/resource/scripts/imex-configure.sh --nodes /root/rack1.list"
```

`imex-configure.sh` 动作：校验本机在列表中且行数为 18 → 写 `nodes_config.cfg` → 设置 `config.cfg` 的 `BIND_INTERFACE_IP` / `SERVER_PORT` / `IMEX_CMD_*` → 确认 channel0 → `systemctl restart nvidia-imex` → 打印 `nvidia-imex-ctl -q`。
参数：`--bind-ip` `--port` `--cmd-port` `--expect N` `--no-restart` `--force`。

> 18 节点 `nodes_config.cfg` 必须字节一致（顺序含），否则 `nvidia-imex-ctl -N` 显示 `!M!`。改列表需全域 restart。

## 4. 验证

```bash
/opt/resource/scripts/imex-check.sh            # L0 包版本 → L1 本机 daemon → L2 domain 矩阵 → L3 fabric
/opt/resource/scripts/imex-check.sh --nodes /root/rack1.list   # 额外 ssh 比对各节点 nodes_config md5
```

通过标准：`nvidia-imex-ctl -N` 全 READY、18×18 矩阵全 `C`、`Domain State: UP`；`-D` 每 GPU `Probe State: Complete` 且 Cluster UUID 一致；`-sanity` 无 FAIL；`nvidia-smi -q` Fabric `State: Completed`。

Probe 未 Complete 属交换机侧（NMX-C / 线缆）问题，节点侧无法修复。

## 5. 一键测试

```bash
/opt/resource/scripts/imex-bench.sh --nodes /root/rack1.list
# 可选：--only t1,t3  --gpus-per-node 4  --max-bytes 4G  --skip-check  --mpi-args "..."
```

| 档 | 工具 | 证明 |
|---|---|---|
| T1 | `imex_fabric_ipc_test`（[源码](../build-iso/scripts/imex_fabric_ipc_test.c)）：rank 0 导出 FABRIC 句柄，其他节点导入、经 NVLink 写入、rank 0 校验，并给出写带宽 | IMEX 本身可用，与 NCCL 无关 |
| T2 | `nvbandwidth -t multinode_device_to_device_memcpy_{read,write}_ce` | 数据面走 NVLink，带宽达标 |
| T3 | `sendrecv_perf` / `alltoall_perf` / `all_reduce_perf`，`NCCL_MNNVL_ENABLE=1` 与 `=0` 对照 | NCCL 识别 72 GPU clique，busbw 明显高于 IB 基线 |

报告：`/var/log/metal-deployer/imex-bench-<ts>.log`（末尾汇总表），原始日志在同名目录。前置：节点间 root 免密 SSH、`imex-check.sh` 全 PASS。

`imex-bench.sh` 用于现场手工排查。批量验收中，整个 rack 的 GPU 对可达性、带宽矩阵和 Fabric 并发测试由 DCGM `mnnvbandwidth` / `nvloom` 负责，见 [gb300-acceptance-plan.md](./gb300-acceptance-plan.md) 的 S7b。

## 6. 配置键速查（`/etc/nvidia-imex/config.cfg`）

| 键 | 默认 | 说明 |
|---|---|---|
| `IMEX_NODE_CONFIG_FILE` | `/etc/nvidia-imex/nodes_config.cfg` | 节点列表 |
| `SERVER_PORT` | 50000 | daemon 间 gRPC |
| `BIND_INTERFACE_IP` | 空（全部） | 多网卡时绑管理网 IP |
| `IMEX_CMD_ENABLED` / `IMEX_CMD_PORT` / `IMEX_CMD_BIND_INTERFACE_IP` | 1 / 50005 / 空 | `nvidia-imex-ctl` 命令服务；需跨节点可达才能从任一节点查全域 |
| `LOG_FILE_NAME` / `LOG_LEVEL` | `/var/log/nvidia-imex.log` / 4 | 日志 |
| `IMEX_WAIT_FOR_QUORUM` | RECOVERY | 非正常退出后等待导入方重连 |

参考：[IMEX Guide — Getting Started](https://docs.nvidia.com/multi-node-nvlink-systems/imex-guide/gettingstarted.html)、[nvidia-imex-ctl](https://docs.nvidia.com/multi-node-nvlink-systems/imex-guide/cmdservice.html)、[配置项](https://docs.nvidia.com/multi-node-nvlink-systems/imex-guide/config.html)。
