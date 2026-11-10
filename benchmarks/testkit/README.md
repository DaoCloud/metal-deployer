# testkit：GB300 批量测试工具（mtest）

> 状态：开发中。已可用 `init`、`import`、`validate`、`plan`、`doctor`、`nic`、`net`、`run`、`status`、`logs`、`cancel`、`report`；目前可运行的测试项：`smoke`、`dcgm`、`superbench.single_node/pair/rack/all`；其余测试项按 [实施计划](#实施进度) 逐步实现。

`mtest` 在一台控制机上经 SSH 管理一批 GB300 节点：识别网卡、下发网络配置，然后在各节点用 Docker 容器运行 DCGM、SuperBench、IMEX、SHARP、MoE 等测试，把结果拉回本机，生成一个 HTML 报告。设计见 [gb300-acceptance-plan.md](../../docs/gb300-acceptance-plan.md)。

## 运行方式

两种方式使用同一份代码：

| 方式 | 依赖 | 说明 |
|---|---|---|
| 直接运行 | Python ≥ 3.8、PyYAML、`ssh`；可选 `ssh-keygen`（mpi 模式）、`rsync`、`sb` | `pip install ./benchmarks/testkit` 后使用 `mtest`；开发时也可 `cd benchmarks/testkit && python3 -m mtest` |
| 容器 | 能运行容器即可 | testkit 镜像内含 mtest、`sb` 与 GB300 配置（P9 提供） |

本机不需要 Docker：测试容器都经 `ssh <节点> docker ...` 在节点上启动。

## 配置文件

| 文件 | 内容 |
|---|---|
| `cluster.yaml` | SSH 参数、网络统一参数、主机列表（rack、tray、LOM 地址、BF3/IPoIB 地址、tags、单机 env/skip） |
| `tests.yaml` | 测哪些项、在哪些节点、怎么分组、参数 |

`mtest init` 生成带注释的模板，字段说明都写在模板注释里。`mtest validate` 检查：

- cluster.yaml：主机名、rack+tray 唯一；地址格式（必须带前缀长度）；所有地址全局唯一；同一 rail 的 IPoIB 在同一子网；mgmt 与网关同网段；最多一台 `golden`；拼错或多余的字段给出警告。
- tests.yaml：主机选择能解析；镜像已配置；模板变量拼写正确；`scope`、`runtime` 取值合法；依赖 MNNVL 的项未启用 imex 时给出警告。
- `--ssh`：并发检查每台主机能否 SSH 登录。

### 主机选择与分组

- `hosts` / `groups` 写法：`all`、rack 名（`R1`）、主机名、`tag:<名称>`，可写成列表或逗号分隔。
- `scope`：`node` 每台一组；`rack` 每个 rack 一组；`group` 选中的主机合成一组，或用 `groups` 列出多组。
- 组内 rank 按 rack、tray 排序，rank 0 的 mgmt 地址为 `MASTER_ADDR`，`MASTER_PORT` = `master_port_base` + 组序号。
- 主机的 `skip` 可写测试项或前缀（`moe` 跳过全部 moe 项）。命令行 `--hosts`、`--exclude` 只能在配置基础上缩小范围。

### 变量与环境

`env`、`cmd`、`args` 中的 `{变量}` 按节点渲染，可用变量见 tests.yaml 开头注释。`{mgmt_if}`、`{ib_hcas}`、`{ib_ucx_devs}` 来自 S3 网卡识别结果，识别前在 `mtest plan` 中显示为 `<S3:...>`。环境变量按以下顺序合并，后者覆盖前者：`common_env` → `<段>.common.env` → `<项>.env` → `per_rank` → cluster.yaml 中该主机的 `env`。

## 导入 IP 规划表

```bash
mtest import gb300-nic-ip-plan.xlsx [--sheet hosts] [--dry-run]   # 或 .csv
```

把规划表读进 `cluster.yaml`（`-c` 指定）的 `hosts` 段：只替换 `hosts`，`ssh`、`network` 和注释保持不变；已有主机的 `tags`、`env`、`skip`、`gpus` 保留；原文件备份为 `cluster.yaml.bak`。`cluster.yaml` 不存在时新建，network 段只保留按规划表下发所需的设置（不改 BF3 MTU，网关和存储路由按主机取自规划表）。写入前先校验，有错误时不写。`.xlsx` 需要 openpyxl（`pip install './benchmarks/testkit[xlsx]'`），否则把 sheet 另存为 CSV。

| 规划表列 | cluster.yaml 字段 |
|---|---|
| `hostname`、`rack`（数字转为 `R<n>`）、`tray`、`sn` | `name`、`rack`、`tray`、`serial` |
| `lom_ip`、`lom_gateway` | `ssh`、`lom_gateway` |
| `bf3_mgt_ip`、`bf3_mgt_gateway`、`bf3_mgt_pci`、`bf3_mgt_mac` | `mgmt`、`mgmt_gateway`、`mgmt_pci`、`mgmt_mac` |
| `bf3_storage_ip`、`_gateway`、`_routes`、`_pci`、`_mac` | `storage`、`storage_gateway`、`storage_routes`、`storage_pci`、`storage_mac` |
| `ib_rail<N>_pci`、`ib_rail<N>_guid`、`ib_rail<N>_ip`（可选） | `ib_pci`、`ib_guid`、`ib`（rail N 对应列表第 N 项） |
| `ib_mtu`（需在表中新增此列） | `ib_mtu` |

其余列（`bmc_*`、`bf3_oob_ip` 等）忽略。导入后会列出各主机缺少的 PCI 与 `ib_mtu`。

## 网卡识别与网络配置（S3、S4）

```bash
mtest nic discover [--hosts R1]         # S3：识别网卡，与 network.expect、金样本比对
mtest nic show [host]                   # 查看一台主机（默认金样本）的映射与检查结果
mtest net apply --dry-run               # S4：只显示每台主机的动作和 netplan diff
mtest net apply [--hosts R1] [--yes]    # 下发并验证
mtest net verify                        # 只验证，不修改
```

主机上需要 python3（Ubuntu 自带）、netplan、systemd-run；读取固件设置需要 `mlxconfig`（没有时跳过 IB 模式与 BF3 模式检查）。

**S3 识别**（`node_scripts/nic_discover.py`，经 SSH 在主机上执行，只读）：

- 按 PCI ID 找出 ConnectX-8（默认 `15b3:1023`）与 BlueField-3（`15b3:a2dc`）；其他 `15b3` 功能（PCIe 交换、DDI、BF3 SoC 管理口）忽略。
- rail N = PCI 树上离 GPU N 最近的 CX8（没有 GPU 时按 PCI 地址排序）；记录 RDMA 设备、IPoIB 接口、NUMA、端口状态与速率、`LINK_TYPE_P1`。
- BF3 两个口按 PCI function 排序，`mgmt.port`、`storage.port` 选择哪个口；BF3 模式取 `INTERNAL_CPU_OFFLOAD_ENGINE`（`DISABLED(1)` 为 NIC 模式）。
- LOM = 承载本次 SSH 会话的接口，同时记录它的默认网关和控制机地址。
- 检查：GPU、CX8、BF3 口数量，链路层、端口 ACTIVE、速率，BF3 模式，与金样本的 rail↔GPU↔RDMA 设备↔NUMA 映射一致。结果写入 `runs/nic/<主机>.json`，测试项中的 `{mgmt_if}`、`{ib_hcas}` 由此填充。

**S4 下发**：每个口按规划表中的 PCI 地址（`mgmt_pci`、`storage_pci`、`ib_pci`）在主机上找到对应网卡，生成 `/etc/netplan/60-mtest.yaml`：

| 内容 | 写法 |
|---|---|
| BF3 业务口（`mgmt_pci`） | `mgmt` 地址、默认路由（`mgmt_gateway`，否则 `network.mgmt.gateway`）、DNS；`network.mgmt.mtu` 设置时才改 MTU |
| BF3 存储口（`storage_pci`） | `storage` 地址、`storage_routes`（经 `storage_gateway`，没有网关时直连），不设默认路由；`network.storage.mtu` 设置时才改 MTU |
| IB（`ib_pci`） | MTU 取 `ib_mtu`（否则 `network.ipoib.mtu`）；有 `ib` 地址时配地址，没有时只设 MTU |
| LOM | 在**已有**的 LOM netplan 条目（同一个 id）下追加固定路由（经 LOM 网关），DHCP 时加 `dhcp4-overrides: {use-routes: false}`，mgmt 配了 DNS 时同时关闭 DHCP 的 DNS/NTP。LOM 不由 netplan 管理时拒绝下发 |

- `network.require_pci: true`（默认）：缺少 PCI、PCI 在主机上找不到或类型不对（BF3 口 / CX8）、规划表的 MAC 或 GUID 与主机不一致时，拒绝下发该主机。`false` 时没有 PCI 的口按 S3 推断（BF3 .0 = 业务、.1 = 存储，rail 按 GPU 顺序）。
- 固定路由：`lom.pin_routes` 为空、且默认路由要移到业务口、而控制机不在 LOM 直连网段时，自动加一条到控制机地址的 /32 路由，保证 SSH 不断。
- netplan 按永久 MAC 匹配 BF3 口（`match_by: mac`，MAC 由 PCI 查到）；虚拟机等没有永久 MAC 的环境用 `match_by: name`。
- S3 也会使用规划表的 PCI：`{mgmt_if}` 取 `mgmt_pci` 对应的网卡；检查项 `plan_pci` 列出找不到的 PCI，以及 `ib_pci` 顺序与按 GPU 距离得到的 rail 顺序不一致的情况。

每台主机的流程：

1. 预检：S3 结构性检查通过（数量、映射、LOM）；没有其他 netplan 条目已经匹配 BF3 口或 IPoIB 接口；没有未确认的上一次修改。内容与现有文件相同则不做修改，只验证。
2. CX8 不是 IB 模式时用 `mlxconfig` 设置 `LINK_TYPE_P1=IB`，需要重启：`ib_mode.reboot` 为 `never` 时标记失败，`ask` 时询问（或 `--yes`），`auto` 时重启并等待主机回来。
3. 备份 `/etc/netplan`，写入新文件，`netplan generate` 失败则恢复；启动 `systemd-run` 定时器，`rollback_seconds` 秒后恢复备份并重新 apply；后台执行 `netplan apply`。
4. 经 LOM 重新连接，执行关键检查：到控制机的路由走 LOM、只有一条经 mgmt 的默认路由、mgmt 地址与 MTU、能 ping 通 mgmt 网关。全部通过才停止回滚定时器；否则等待自动回滚并确认已恢复，记为 fail。
5. 全部主机完成后做完整检查：storage、IPoIB 地址与 MTU，IB 端口 ACTIVE 与速率，经 mgmt、storage、每个 rail ping 同 rack 的下一台主机（rack 内只有一台时选其他主机）。完整检查失败记为 fail，但保留配置。

`--dry-run` 只做第 1、2 步的判断并打印 netplan diff。只支持默认分区 `pkey 0x7fff`。

## 执行测试

```bash
mtest run                         # 按内置顺序执行全部 enabled 项
mtest run smoke dcgm --hosts R1   # 只执行部分项、部分主机
mtest run --force                 # 不排除之前失败的主机
mtest status [--host <name>]      # 每项的通过/失败数、失败主机
mtest logs <run-id> [--host <name>] [--item <item>] [--follow]
mtest cancel <run-id>             # 或在运行终端按 Ctrl-C / kill
```

每组的执行过程：

1. 加锁：`runs/locks/<主机>.lock`，同一主机同时只能属于一次运行；持锁进程已退出时自动接管。
2. 预检（并发）：SSH 与 Docker 可用；镜像存在（按 `image_pull` 拉取、推送或只检查）；没有其他 `acc-*` 测试容器在运行；建立节点结果目录。任一主机不通过，整组不启动。
3. 启动：容器名 `acc-<run-id>-<项>-<主机>`，带 `mtest.run` 等标签；参数为 `runtime.container_args`（可用 `{变量}`）+ 结果目录挂载到 `/results` + 环境变量。
4. 监控：每 `poll_interval` 秒查询容器状态。组内任一主机失败，立即停止本组其余容器；超过 `run_timeout`、`mtest cancel`、Ctrl-C 或 SIGTERM 时停止全部容器。连续 5 次连不上的主机记为 error。
5. 收集：保存容器日志，拉回节点结果目录（有 rsync 用 rsync，否则 ssh + tar），写每台主机的 `summary.json` 和组的 `summary.json`。
6. 清理：删除容器，释放锁。

三种启动方式：

| 方式 | 做法 | 用于 |
|---|---|---|
| per-node | 每台主机一个容器，各自执行命令后退出，按 `RANK`/`MASTER_ADDR` 汇合 | MoE、DCGM |
| mpi | 每台主机一个运行 sshd 的容器（本次运行生成的临时密钥，端口 `mpi_ssh_port`）；确认各节点 sshd 可连后，在 rank 0 容器内执行启动命令（mpirun），hostfile 在 `{hostfile}` | IMEX、SHARP |
| delegate | 只在组内 rank 0 主机启动一个容器，由它自行分发 | SuperBench |

结果状态：`pass`（退出码 0）、`fail`（1，或 summary.json 报告失败）、`error`（其他退出码、预检失败、连接丢失、结果未拉回）、`timeout`、`stopped`（因组内其他主机失败被停止）、`blocked`（主机被其他运行占用）、`cancelled`。`runtime.stop_on_failure` 为 true 时，在前序测试项中因自身原因 fail/error/timeout 的主机不进入后续测试项；整组共同的结果（mpi、delegate 的组结果，被连带停止）不会排除主机。

结果目录（`runtime.results_dir`，默认 tests.yaml 同级的 `runs/`）：

```
runs/
├── state.json                          # 每台主机每项的最新结果
├── locks/
└── <run-id>/
    ├── run.json                        # 本次运行：参数、分组、rank、每组状态
    ├── <主机>/<项>/summary.json          # 拉回的结果 + container.log（mpi 另有 launcher.log）
    └── _groups/<项>/<组>/summary.json
```

## 报告

```bash
mtest report [--out DIR] [--title T] [--archive]
```

只读本地结果目录，不连主机，可随时重新生成。默认写到 `<results_dir>/report/`：

| 文件 | 内容 |
|---|---|
| `report.html` | 单个文件，数据、样式、图表全部内嵌，离线可打开 |
| `hosts.csv` | 每台主机一行：总体结论、各测试项状态、失败原因 |
| `metrics.csv` | 每条指标一行：run、测试项、组、主机、指标、数值、单位 |
| `raw-logs.tar.gz` | `--archive` 时打包所有 run 目录 |

每台主机每个测试项取 `state.json` 中的最新结果。页面：总览（rack × tray 矩阵）、问题与离群值、各测试项（主机 × 指标表，按偏离全部主机中位数着色；多节点项另有组卡片）、主机详情、版本一致性、执行记录。

判定规则：主机“有自身失败项”只看它自己的结果（fail / error / timeout）；mpi、delegate 的组结果和被连带停止都记在组上，不改变主机结论。性能指标不设门限，只按偏离中位数着色提示。

测试容器写 `/results/summary.json` 即可进入报告，格式：

```json
{"status": "pass",
 "metrics": {"gemm_fp8": {"value": 4510.3, "unit": "TFLOPS", "category": "GPU-COMPUTE", "lower_better": false}},
 "checks": {"ecc": {"result": "pass", "reason": ""}}}
```

可选 `/results/env.json`（`{"driver": "580.95.05", ...}`）用于版本一致性页。组级 `summary.json` 可带 `tables`、`charts`、`matrix`，在组卡片中显示。

### dcgm：S5 DCGM 诊断

使用官方镜像 `nvcr.io/nvidia/cloud-native/dcgm:4.7.1-ubuntu24.04`（含 arm64、CUDA 13 诊断插件，与 ISO 中 DCGM 4.7.1 一致），不另做镜像。mtest 把包装脚本 `node_scripts/dcgm_run.sh` 写到主机并挂载进容器，脚本只采集原始输出，判定在控制端完成（`mtest/parsers/dcgm.py`）。

| 采集 | 判定 |
|---|---|
| `dcgmi diag -r <level> -j` | 每个诊断项一个检查：Fail 为失败（附失败 GPU 与告警），Skip 记为跳过；`runtime_error` 记为失败 |
| `nvidia-smi -q` | GPU 数量；未纠正 ECC（Volatile 各项之和）；Remapped Rows 未纠正、Pending、失败；Fabric `State: Completed` / `Status: Success` |
| `nvidia-smi nvlink -s` | 每张 GPU 的 NVLink 全部 up |
| `dcgmi health` | Overall Health 为 Healthy |

`dcgm.checks` 选择启用哪些 nvidia-smi 检查，`health: false` 关闭 health。版本写入 `env.json`（driver、CUDA、vbios、DCGM），进入报告的版本一致性页；诊断日志在 `raw/diag.log`。

`hostengine` 两种模式（命令都在容器里执行，区别是诊断由哪个 hostengine 跑）：

- `container`（默认）：容器内启动 nv-hostengine，用镜像自带的 DCGM 与诊断插件，所有主机版本一致，不依赖主机安装的 DCGM。同一时间只能有一个 hostengine 使用 GPU，mtest 在启动前停掉主机的 `nvidia-dcgm`（或 `dcgm`）服务，结束后（包括失败、取消）再启动；这段时间依赖主机 DCGM 的监控（如 dcgm-exporter）中断。
- `host`：容器以主机网络连接主机上的 `nvidia-dcgm` 服务（127.0.0.1:5555），诊断由主机的 DCGM 执行，不停主机服务；主机服务未运行时该主机记为 error。

`gpu_burn` 尚未接入 dcgm 项，开启时记为跳过。

### superbench：S6 SuperBench 与 RDMA 打流

每组在 rank 0 主机上启动 sbcli 镜像（`images.sbcli`，需与主机同为 arm64），由它对组内所有主机执行 `sb deploy` / `sb run`（脚本 `node_scripts/sb_run.sh`）。worker 镜像 `images.sb_worker` 须已在每台主机上（ISO 预置），预检会逐台检查。

- inventory：名称为主机的 mgmt 地址（MPI 走业务网），`ansible_host` 为 LOM 地址（SSH）。
- SSH 密钥：本次运行生成的密钥在预检时加入每台主机的 `authorized_keys`，结束后（包括失败、取消）删除，rank 0 主机上的私钥同时删除；不会把 `cluster.yaml` 中的密钥拷到主机。
- `benchmarks.include` / `exclude` 改写配置的 `superbench.enable`。
- `timeout`（默认 14400 秒）覆盖 `runtime.run_timeout`。

| 测试项 | 配置 | 结果 |
|---|---|---|
| `single_node` | `config_gb300_single_node.yaml` | 所有主机一次执行，按主机拆分：每个 benchmark 的 `return_code` 为检查项，非 0 时该主机失败；每块 GPU 的指标取最差值；NCCL、ib-loopback 只在报告中显示最大消息的数值，其余写入 `metrics.csv` |
| `pair` / `rack` / `all` | `config_gb300_multi_node.yaml` | 组结果：NCCL busbw 表与随消息大小的曲线；ib-traffic 按节点对、rail 解析 |

报告的“RDMA 打流”页取 `superbench.all`（否则 `rack`，再否则 `pair`）的 ib-traffic 结果，画出每个 rail 的节点对矩阵（带宽、双向带宽、时延），失败的测量（-1）显示为黑格。

磁盘测试（`superbench.disk`）在 P10 实现。

### smoke：检查节点能否运行测试容器

`smoke` 在每台主机启动一个容器，写出 `summary.json` 后退出，用来确认 SSH、Docker、镜像都正常。`mode: mpi` 时在组内跑一次 `mpirun hostname`，用来确认 mpi 模式（sshd、hostfile、跨节点 SSH）可用。`sleep`、`exit_code` 用于演练；在某台主机的 `env` 中设置 `MTEST_SMOKE_EXIT: "1"` 可让它单独失败。

## 快速体验（不需要真实节点）

```bash
cd benchmarks/testkit
python3 -m mtest init --dir /tmp/mtest-demo --racks 2 --trays 18   # 生成 36 台示例
cd /tmp/mtest-demo
PYTHONPATH=$OLDPWD python3 -m mtest validate
PYTHONPATH=$OLDPWD python3 -m mtest plan                 # 全部启用项的分组与 rank
PYTHONPATH=$OLDPWD python3 -m mtest plan moe.rack -v     # 每个节点渲染后的 env 与命令
PYTHONPATH=$OLDPWD python3 -m mtest plan --hosts R2 --exclude gb300-r2-t05
PYTHONPATH=$OLDPWD python3 -m mtest doctor
```

用 `pip install ./benchmarks/testkit` 安装后，以上命令直接写 `mtest ...`。

## 命令

```bash
mtest init | validate | plan | doctor
mtest nic discover | nic golden <host>      # S3
mtest net apply [--dry-run]                 # S4
mtest run [item ...] [--hosts R1] [--exclude h1,h2]   # S5–S8
mtest status | logs <run-id> | cancel <run-id>
mtest report                                # S9
```

## 开发

```bash
make test-testkit       # 或 cd benchmarks/testkit && python3 -m unittest discover -s tests -v
benchmarks/testkit/e2e/run-e2e.sh [--keep]       # 执行器端到端测试，约 3 分钟
benchmarks/testkit/e2e/run-net-e2e.sh [--keep]   # S3/S4 端到端测试，约 4 分钟
```

端到端测试不需要 GPU 节点：在本机用 Docker 启动 3 个“假节点”（sshd + docker CLI，使用本机 Docker），再经真实 SSH 执行 `mtest`，覆盖 per-node、mpi、delegate 三种方式、单台失败、超时、`mtest cancel`、SIGTERM、`status`、`logs`、`report`，以及用官方 DCGM 镜像跑 `dcgm`（无 GPU，验证脚本下发、容器内 hostengine、原始输出拉回与解析、主机模式缺少 hostengine 时报错），和用 sb 替身镜像跑 `superbench`（替身经真实 SSH 用本次运行的密钥连每台主机并检查 worker 镜像，输出 SuperBench 格式结果；验证 include/exclude、按主机拆分、ib-traffic 节点对、运行后密钥删除、报告 RDMA 矩阵）。需要本机有 Docker，首次运行会拉取 `docker:cli`、`debian:bookworm-slim` 并安装 OpenMPI。网段默认 172.28.0.0/24、172.29.0.0/24，冲突时用 `SSH_NET`、`MGMT_NET` 环境变量修改。

S3/S4 端到端测试启动 3 台 systemd 容器（netplan、systemd-networkd、sshd），LOM 由 dnsmasq 经 DHCP 分配地址，另有 mgmt、storage 和 4 个 rail 网络；容器内用假的 `/sys` 把 veth 映射成 BF3 与 CX8。主机由 `mtest import` 导入 CSV 规划表（含 PCI、网关、存储路由、`ib_mtu`）。覆盖：导入、识别与金样本比对、dry-run、按 PCI 下发（默认路由、LOM 固定路由与保留 DHCP 地址、BF3 MTU 不变、IB MTU、存储路由、跨 rail ping）、重新识别后重复执行无变更、错误网关触发自动回滚并恢复、`net verify`、规划表 PCI 在主机上不存在时拒绝、冲突条目拒绝下发、报告。需要能拉取 ubuntu:24.04（`UBUNTU_IMAGE` 可改为镜像站），网段默认 172.28.10–43.0/24（`NET_PREFIX` 可改）。

只用标准库和 PyYAML，语法保持兼容 Python 3.8。

## 实施进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 包结构、命令框架、测试 | 完成 |
| P1 | 配置加载与校验、主机选择、分组与 rank、`init/validate/plan/doctor` | 完成 |
| P2 | 执行器：SSH、远程 Docker、三种启动模式、结果拉回、`run/status/logs/cancel` | 完成 |
| P6 | HTML 报告 | 完成 |
| P3 | S3 网卡识别、S4 网络配置 | 完成（实机待确认） |
| P4 | S5 DCGM | 完成（实机待确认） |
| P5 | S6 SuperBench（含 RDMA 打流） | 完成（实机待确认） |
| P7 | S7 IMEX、S7a SHARP | 待做 |
| P8 | S8 MoE | 待做 |
| P9 | testkit 镜像、文档 | 待做 |
| P10 | SuperBench 磁盘测试（安全检查） | 待做 |
