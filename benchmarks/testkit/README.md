# testkit：GB300 批量测试工具（mtest）

> 状态：开发中。已可用 `init`、`import`、`validate`、`plan`、`doctor`、`nic`、`net`、`run`、`status`、`logs`、`cancel`、`report`；目前可运行的测试项：`smoke`、`links`、`dcgm`、`superbench.*`、`imex`、`nvlink.*`、`sharp.scale_out`、`nvshmem.*`、`nixl.*`；其余测试项按 [实施计划](#实施进度) 逐步实现。

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
| `bf3_mgt_ip`（或 `mgmt_ip`）、`bf3_mgt_gateway`、`bf3_mgt_pci`、`bf3_mgt_mac` | `mgmt`、`mgmt_gateway`、`mgmt_pci`、`mgmt_mac` |
| `bf3_storage_ip`（或 `storage_ip`）、`_gateway`、`_routes`、`_pci`、`_mac` | `storage`、`storage_gateway`、`storage_routes`、`storage_pci`、`storage_mac` |
| `ib_rail<N>_pci`、`ib_rail<N>_guid`、`ib_rail<N>_ip`（可选；也可写 `rail<N>_*`） | `ib_pci`、`ib_guid`、`ib`（rail N 对应列表第 N 项） |
| `ib_mtu`（需在表中新增此列） | `ib_mtu` |

表头按 GB300 的 IP 规划表命名；没有 BF3 的机型可用括号中与硬件无关的列名（`bf3_*` 前缀去掉、`mgmt_ip`、`storage_ip`、`rail<N>_*`），两种写法等价。其余列（`bmc_*`、`bf3_oob_ip` 等）忽略。导入后会列出各主机缺少的 PCI 与 `ib_mtu`。

## 网卡识别与网络配置（S3、S4）

```bash
mtest nic discover [--hosts R1]         # S3：识别网卡，与 network.expect、金样本比对
mtest nic show [host]                   # 查看一台主机（默认金样本）的映射与检查结果
mtest net apply --dry-run               # S4：只显示每台主机的动作和 netplan diff
mtest net apply [--hosts R1] [--yes]    # 下发并验证
mtest net verify                        # 只验证，不修改
```

主机上需要 python3（Ubuntu 自带）、netplan、systemd-run；读取固件设置需要 `mlxconfig`（没有时跳过 IB 模式与 BF3 模式检查）。

**硬件 profile**（`network.profile`，默认 `gb300`）决定 `network.expect` 与 mgmt、storage 两个角色的默认值；cluster.yaml 中写出的键覆盖 profile：

| profile | mgmt | storage | rail（`expect.cx8`） |
|---|---|---|---|
| `gb300` | BF3 port 0（`device: bf3`） | BF3 port 1 | 4 × CX8 `15b3:1023`，InfiniBand，800 Gb/s；另要求 4 GPU、BF3 两个口为 NIC 模式 |
| `custom` | `device: nic`：按 `mgmt_pci` 找到的任意网卡；或 `device: lom`：mgmt 地址就是 LOM 地址，S4 不配置 mgmt、不移动默认路由 | `device: nic`，按 `storage_pci` 找 | 不预设，按现场在 `expect` 中写数量、`pci_id`、链路层、速率；没有 BF3 时不写 `expect.bf3` |

`device: nic` 只能按 PCI 定位，与 `require_pci` 无关，规划表必须给出 PCI。示例（没有 DPU、mgmt 在普通网卡上的机型）：

```yaml
network:
  profile: custom
  expect:
    gpus: 8
    cx8: {count: 8, pci_id: "15b3:1023", link_layer: InfiniBand, rate_gbps: 800}
  mgmt: {device: nic, default_route: true}
  storage: {device: nic}
```

**S3 识别**（`node_scripts/nic_discover.py`，经 SSH 在主机上执行，只读）：

- 按 PCI ID 找出 ConnectX-8（默认 `15b3:1023`）与 BlueField-3（`15b3:a2dc`）；其他 `15b3` 功能（PCIe 交换、DDI、BF3 SoC 管理口）忽略。mgmt/storage 为 `device: nic` 时，另按规划表中的 PCI 读取该网卡（任意厂商）的接口名与 MAC。
- rail N = PCI 树上离 GPU N 最近的 CX8（没有 GPU 时按 PCI 地址排序）；记录 RDMA 设备、IPoIB 接口、NUMA、端口状态与速率、`LINK_TYPE_P1`。
- BF3 两个口按 PCI function 排序，`mgmt.port`、`storage.port` 选择哪个口；BF3 模式取 `INTERNAL_CPU_OFFLOAD_ENGINE`（`DISABLED(1)` 为 NIC 模式）。
- LOM = 承载本次 SSH 会话的接口，同时记录它的地址与前缀、网关（S4 之后从策略路由表中取）以及控制机地址。
- 检查：GPU、CX8、BF3 口数量，链路层、端口 ACTIVE、速率，BF3 模式，与金样本的 rail↔GPU↔RDMA 设备↔NUMA 映射一致。结果写入 `runs/nic/<主机>.json`，测试项中的 `{mgmt_if}`、`{ib_hcas}` 由此填充。

**S4 下发**：每个口按规划表中的 PCI 地址（`mgmt_pci`、`storage_pci`、`ib_pci`）在主机上找到对应网卡，写两个文件：

| 文件 | 内容 |
|---|---|
| `/etc/netplan/90-static-mgmt-storage.yaml` | 管理口（`mgmt_pci`）：`mgmt` 地址、默认路由（`mgmt_gateway`，否则 `network.mgmt.gateway`）、DNS；存储口（`storage_pci`）：`storage` 地址、`storage_routes`（经 `storage_gateway`，没有网关时直连），不设默认路由；`network.<mgmt|storage>.mtu` 设置时才改 MTU。默认路由移到 mgmt 时还包含 LOM 策略路由（见下） |
| `/etc/netplan/90-static-compute.yaml` | 算力网卡 IPoIB（`ib_pci`）：MTU 取 `ib_mtu`（否则 `network.ipoib.mtu`）；有 `ib` 地址时配地址，没有时只设 MTU |

- mtest 只写、只删这两个文件；此外只会按下方规则改写 LOM 所在的 netplan 文件。没有内容的文件不写。
- `network.require_pci: true`（默认）：缺少 PCI、PCI 在主机上找不到或类型不对（BF3 口 / 普通网卡 / CX8）、规划表的 MAC 或 GUID 与主机不一致时，拒绝下发该主机。`false` 时没有 PCI 的 BF3 口与 rail 按 S3 推断（BF3 .0 = 业务、.1 = 存储，rail 按 GPU 顺序）。
- netplan 按永久 MAC 匹配管理口、存储口（`match_by: mac`，MAC 由 PCI 查到）；虚拟机等没有永久 MAC 的环境用 `match_by: name`。

**LOM 策略路由**：LOM 在 mtest 接入前已由 netplan 静态配置（地址、通常还有默认路由）。默认路由移到 mgmt 后，从 LOM 进来的连接（控制机 SSH，以及任何经 LOM 访问主机的网段）的回包若按主路由表走 mgmt，会因路径不对称被丢弃。S4 因此：

1. 先建策略路由：在 `90-static-mgmt-storage.yaml` 中，用 LOM **已有**条目的 id（另起一个匹配同一网卡的 id 会被 networkd 忽略）合并：

   ```yaml
   eno1:                                   # LOM 原有条目的 id
     routes:
       - {to: 10.10.0.0/24, scope: link, table: 100}
       - {to: default, via: 10.10.0.1, table: 100}   # 仅当 LOM 文件里没有默认路由；网关取 lom_gateway，否则 S3
     routing-policy:
       - {from: 10.10.0.11/32, table: 100, priority: 100}
   ```

2. 再从 main 表删掉 LOM 默认路由：netplan 合并时列表只追加，别的文件删不掉它，所以 S4 改写 LOM 所在的文件，把该条目的 `gateway4` 或主表 `to: default` 路由改为 `table: 100`（等于搬进策略表），其余内容不变（文件中的 YAML 注释不保留，文件头加一行说明）。改写前整个 `/etc/netplan` 已备份，回滚时一起恢复；下发时若该文件已被他人改动则拒绝。再次执行时路由已在表 100 中，不再改写。

- 源地址为 LOM 地址的流量查表 `network.lom.table`（默认 100），从 LOM 出去；其他流量按主表走 mgmt 默认路由。旧的 `lom.pin_routes` 不再使用（写了会提示）。
- 拒绝下发的情况：LOM 不由 netplan 管理；LOM 条目为 DHCP（mtest 要求静态）；LOM 条目不含 SSH 所用地址；需要由 mtest 加表默认路由时网关未知或不在 LOM 网段。
- `mgmt.default_route: false`、mgmt 没有网关或 `mgmt.device: lom` 时默认路由留在 LOM，LOM 不做任何改动。
- S3 也会使用规划表的 PCI：`{mgmt_if}` 取 `mgmt_pci` 对应的网卡；检查项 `plan_pci` 列出找不到的 PCI，以及 `ib_pci` 顺序与按 GPU 距离得到的 rail 顺序不一致的情况。

每台主机的流程：

1. 预检：S3 结构性检查通过（数量、映射、LOM）；没有其他 netplan 条目已经匹配 BF3 口或 IPoIB 接口；没有未确认的上一次修改。内容与现有文件相同则不做修改，只验证。
2. CX8 不是 IB 模式时用 `mlxconfig` 设置 `LINK_TYPE_P1=IB`，需要重启：`ib_mode.reboot` 为 `never` 时标记失败，`ask` 时询问（或 `--yes`），`auto` 时重启并等待主机回来。
3. 备份 `/etc/netplan`，写入新文件（删除不再需要的 mtest 文件），`netplan generate` 失败则恢复；启动 `systemd-run` 定时器，`rollback_seconds` 秒后恢复备份并重新 apply；后台执行 `netplan apply`。
4. 经 LOM 重新连接，执行关键检查：以 LOM 地址为源到控制机的路由走 LOM（`ip route get <控制机> from <LOM 地址>`，策略路由生效）、只有一条经 mgmt 的默认路由、mgmt 地址与 MTU、能 ping 通 mgmt 网关。全部通过才停止回滚定时器；否则等待自动回滚并确认已恢复，记为 fail。
5. 全部主机完成后做完整检查：storage、IPoIB 地址与 MTU，IB 端口 ACTIVE 与速率，经 mgmt、storage、每个 rail ping 同 rack 的下一台主机（rack 内只有一台时选其他主机）。完整检查失败记为 fail，但保留配置。

`--dry-run` 只做第 1、2 步的判断并打印每个文件的 netplan diff。只支持默认分区 `pkey 0x7fff`。

### bond（设计，尚未实现）

需要组 bond 时（例如 BF3 两个口做 LACP 承载 mgmt），作为 S4 之后单独执行的一步，复用 S4 的引擎，不另写脚本：

- 命令：`mtest net bond apply|verify [--dry-run]`。与交换机侧 LACP 配置要协同，执行时机与 S4 不同，所以单独成命令；但备份、`systemd-run` 定时回滚、关键检查、冲突检测与主机锁全部复用 S4，bond 改动 mgmt 时最容易把主机配失联，正需要这套保护。
- 配置：`network.bonds.<名字>: {members: [mgmt, storage] 或 PCI 列表, mode: 802.3ad, lacp_rate, mii_monitor_interval, transmit_hash_policy, mtu}`；角色可改为引用 bond，例如 `mgmt: {bond: bond0}`，地址与路由随之写到 bond 上。
- 文件：`90-static-bond.yaml`。`90-static-mgmt-storage.yaml` 中成员口的条目改为只做匹配、不配地址，同一次下发中一起更新。
- 验证：`/proc/net/bonding/<bond>` 中所有成员 `MII Status: up`、LACP partner MAC 非全零且各成员一致、bond 速率与 MTU 符合预期，再加 S4 原有的地址、路由与 ping 检查。

## 执行测试

```bash
mtest run                         # 按内置顺序（L0 → L4）执行全部 enabled 项，遇到关卡失败即停
mtest run smoke dcgm --hosts R1   # 只执行部分项、部分主机
mtest run --from nvlink.pairs     # 排障后从某一项接着跑（跳过它之前的 enabled 项）
mtest run --force                 # 不排除之前失败的主机，也不因 rack 不完整而阻断
mtest run --no-gate               # 关卡失败也继续跑后续项
mtest status [--host <name>]      # 每项的通过/失败数、失败主机
mtest logs <run-id> [--host <name>] [--item <item>] [--follow]
mtest cancel <run-id>             # 或在运行终端按 Ctrl-C / kill
```

### 分层与关卡

测试项按验收层级排列，先确认每根线、每个链路，再测整机吞吐。每层除测试结果外，还对比硬件数据（链路状态、错误计数、光模块、内核日志），用来定位到具体的线和端口：

| 层 | 测试项 | 范围 | 命令 | 佐证用的硬件数据 |
|---|---|---|---|---|
| L0 | `links` | 每台主机，只读 | `mtest run links` | NVLink 18 条是否 up、速率；错误计数现值；节点内 P2P；IB 端口状态、速率、计数；`mlxlink` 光模块与 BER；本次开机以来的内核错误 |
| L1 | `dcgm`、`superbench.single_node` | 每台主机 | `mtest run dcgm superbench.single_node` | 前后错误计数增量、链路掉线、新增内核错误；DCGM diag/health；单机指标的主机间离群 |
| L2 | RDMA：`superbench.rack`（阶段 A：柜内相邻 tray 两两、同 rail、一轮）→ `superbench.cross_rack`（阶段 B：R1 第 i 台 ↔ R2 第 i 台） | 每个 rack / rack 之间 | `mtest run superbench.rack superbench.cross_rack` | 前后计数增量（写明 IB 端口与 rail）、内核日志增量；每个（主机, rail）离群 |
| L3 | NVLink：`imex` → `nvlink.pairs`（每对 GPU 能否互访、读写带宽矩阵）→ `nvlink.fabric`（NVLoom） | 每个 rack | `mtest run imex nvlink.pairs nvlink.fabric` | IMEX 矩阵；前后 NVLink 计数增量（写明 GPU 与链路）；带宽矩阵和 NVLoom 中的离群 GPU |
| L4 | NCCL：`superbench.nccl_node`（单机 4 GPU）→ `superbench.nvl_domain`（柜内 MNNVL，含 NVLS）→ `superbench.all`（全部 rack，柜内 NVLink + 柜间 IB）→ `sharp.scale_out`（IB SHARP）→ `nvshmem.*`（可选）→ `nixl.*`（可选）→ `superbench.soak`（长稳，默认关） | 每台主机 / 每个 rack / 全部 | `mtest run --from superbench.nccl_node` | NCCL 日志：每张 GPU 走自己 rail 的 CX8、没有 BF3/RoCE、没有 NET/Socket（`hca_check`）；NVLS、MNNVL、CollNet 生效；小消息时延；前后计数与内核日志只告警 |
| L5 | MoE：`moe.single` → `moe.rack` → `moe.cross_rack` → `moe.all` | 单机 / 柜内 / 跨柜 / 全部 | `mtest run moe` | 前后计数与内核日志只告警 |

- **关卡（`gate`）**：L0–L3 的测试项默认 `gate: true`。这样的项只要不是 pass（fail、error、timeout、blocked……），mtest 就停止本次运行，打印失败的组、主机和原因，以及排障后的续跑命令（`mtest run --from <项>`）。L4、L5 默认不设关卡。每项可在 tests.yaml 中用 `gate: true/false` 覆盖。
- **rack 完整性（`full_rack`）**：rack 级 NVLink 项（`imex`、`nvlink.*`、`superbench.nvl_domain`、`nvshmem.rack`、`moe.rack`）默认要求整个 rack 都在。如果 rack 中有主机因前序失败被排除，该 rack 记为 `blocked`，不会用剩下的主机照跑。`--force` 取消此限制。
- **链路错误计数器与内核日志（`counters`）**：L1–L5 的每个测试项开始前、结束后，mtest 经 SSH 在组内每台主机读一次 NVLink（`nvidia-smi nvlink -e`、`-s`）、IB 端口（`/sys/class/infiniband/*/ports/*/counters`）计数器，以及这段时间的内核日志（`journalctl -k --since`）。计数器增加、链路掉线或出现 `kernel_patterns` 中的内核错误（Xid、GPU 掉卡、mlx5 错误、PCIe AER 等）时：L1–L3 该主机记为 fail，原因中写明 GPU 和链路编号或 IB 端口（有 S3 结果时附 rail 编号）；L4、L5（`warn_layers`）只记为告警。FEC 直方图、BER、原始 symbol 错误不判定（`counters.nvlink_ignore`），IB 只看 `counters.ib_watch` 中的计数器，`kernel_ignore` 可排除个别内核消息。前后快照与差值保存在 `raw/link-counters.json`。`counters.items` 可改为指定测试项（都按 fail 判定），`enabled: false` 关闭。
- **离群判定**：同一指标与组内（rack 或全部主机）中位数比较，差距超过 `pct`（默认 10%）记为离群；`action: fail`（默认）时该主机失败，`warn` 只在报告中显示。`superbench.common.outlier` 作用于单机指标（nvbandwidth、gpu-copy-bw、ib-loopback）和 ib-traffic 每个（主机, rail）的中位数；`nvlink.common.outlier_pct` / `outlier_action` 作用于 GPU 带宽矩阵和 NVLoom 结果。RDMA 阶段 A、B 每台主机只有一个对端，离群时两端都会被标出：一台主机在两个阶段都离群，问题在这台主机；只在一个阶段离群，问题在对端或路径，再结合计数器与 `mlxlink` 判断。
- **基线**：`superbench.*.baseline`（NCCL 最大消息的 busbw 下限，GB/s）和 `nvlink.common.baseline`（每个测试用例的 GPU 对下限）在金样本 rack 实测后填写；低于基线即失败。

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
| host | 不启动容器，经 SSH 在主机上执行（长任务用 `systemd-run` 在后台运行并轮询，取消或超时时停止） | `links`、`nvlink.*` |

结果状态：`pass`（退出码 0）、`fail`（1，或 summary.json 报告失败）、`error`（其他退出码、预检失败、连接丢失、结果未拉回）、`timeout`、`stopped`（因组内其他主机失败被停止）、`blocked`（主机被其他运行占用，或 rack 不完整）、`cancelled`。`runtime.stop_on_failure` 为 true 时，在前序测试项中因自身原因 fail/error/timeout 的主机不进入后续测试项；整组共同的结果（mpi、delegate 的组结果，被连带停止）不会排除主机。

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

### links：L0 链路状态与错误计数（每台主机）

不启动容器，经 SSH 在主机上执行 `node_scripts/link_counters.py`（只读），在控制端判定（`mtest/parsers/links.py`）：

| 采集 | 判定 |
|---|---|
| `nvidia-smi --query-gpu` | GPU 数量等于 `cluster.yaml` 中的 `gpus` |
| `nvidia-smi nvlink -s` | 每张 GPU 至少 `nvlink_per_gpu`（默认 18）条链路 up，列出掉线链路编号；各链路速率一致 |
| `nvidia-smi topo -p2p n` | 节点内 GPU 两两 `OK`（`p2p: false` 关闭） |
| `/sys/class/infiniband/*/ports/*` | IB link layer 的端口数等于 `ib_ports`（默认 4）、全部 ACTIVE、速率不低于 `ib_rate_gbps` |
| `nvidia-smi nvlink -e`、IB `counters/` | 非零的受监控计数器个数作为指标；`max_error_count` 设置时超过即失败 |
| `mlxlink -d <dev> -p <port> -m -c -e --json`（MFT，`mlxlink: true`） | 每个 IB 口：effective physical errors 非 0 即失败；raw/effective BER、光模块温度作为指标，`mlxlink_limits` 设置时超过即失败；没有 mlxlink 时跳过 |
| 内核日志（本次开机以来，`journalctl -k -b`） | 匹配 `counters.kernel_patterns` 的行数作为指标并告警；`max_kernel_errors` 设置时超过即失败 |

快照保存在 `raw/link-counters.json`，与之后各测试项前后的快照格式相同。

### nvlink：S7b NVLink GPU 对与 Fabric（每个 rack 一组，主机模式）

用 ISO 预装的 DCGM multinode 包（`datacenter-gpu-manager-4-multinode{,-cuda13}`）在每个 rack 的 rank 0 主机执行 `dcgmi mndiag`，不用容器：

1. 预检时每台主机：检查 `mpirun` 与 `plugin_dir` 下的 `mnnvbandwidth` / `nvloom_cli`；记录 `nvidia-dcgm` 当前是否运行；写入 `/run/systemd/system/nvidia-dcgm.service.d/90-mtest.conf`，让 nv-hostengine 监听本机 mgmt 地址（`-b <mgmt>`，默认只监听 127.0.0.1）并允许以 root 启动 MPI，重启服务并确认 `dcgmi discovery --host <mgmt>` 可连；把本次运行的公钥加入 `authorized_keys`。rank 0 主机另装本次运行的私钥和 `/etc/ssh/ssh_config.d/90-mtest-<run>.conf`，让 Open MPI 以 root 免密 SSH 到组内其他主机。
2. rank 0 主机用 `systemd-run` 在后台执行 `node_scripts/nvlink_run.py`，依次运行各步骤，每步的 `dcgmi` 输出、DCGM 在这一步写入 `dcgm_log_dir` 的 workload 日志都保存下来；某一步不是 Pass 时（`stop_on_fail`）跳过后续步骤。mtest 轮询 `done.json`，`mtest cancel`、Ctrl-C 或超时时 `systemctl stop` 该单元（DCGM 收到 SIGTERM 会停止诊断）。
3. 结束后（包括失败、取消）每台主机删除 drop-in、私钥和 ssh 配置，恢复 `nvidia-dcgm` 原来的运行状态，从 `authorized_keys` 删除公钥。

| 测试项 | 步骤 | 判定 |
|---|---|---|
| `nvlink.pairs`（L2） | `pairs`：`mnnvbandwidth.gpuPairs=all`，每对 GPU 1 MiB 读（文本输出，含 Pairs Planned/Passed/Failed）；`bandwidth`：正常模式，默认 `targetNumPairs=-1` 测全部有向对的 CE 读、写（`--json`） | Planned = N(N−1)/2 且 Failed = 0；错误中出现的主机记为失败；nvbandwidth 矩阵中每张 GPU（作为源的行、作为目的的列）的中位数与 rack 中位数比较，离群 GPU 所在主机失败；低于 `baseline` 的 GPU 对失败 |
| `nvlink.fabric`（L3） | 每个 NVLoom suite 一步：`gpu-to-rack`、`pairwise`、`all-to-one`、`multicast`、`latency`、`fabric-stress`（`time_to_run` 按 suite 设置，`rich_output` 的 suite 输出每个 GPU 对） | 每个 suite 的 DCGM 结果为 Pass；JSON 中失败的 GPU 记到对应主机；nvloom_cli 输出的每个测试用例按行（GPU）比较中位数，离群 GPU 所在主机失败；延迟类（ns）以高为差 |

DCGM 的 Pass 只表示传输完成，不检查带宽；带宽和延迟由 mtest 从 workload 日志解析（nvbandwidth 的 `rank N (host), device D` 与矩阵；nvloom_cli 的 `Process` 行、矩阵与 `Min/Med/Max`）。主机名经各主机的 `hostname` 与 `cluster.yaml` 地址映射到 mtest 主机名。组卡片显示步骤、测试用例汇总（最小、中位数、最大、离群 GPU 数）、离群 GPU、离群 GPU 对和错误；每台主机的指标为其最差 GPU 的中位数。

实机待确认：nv-hostengine 改监听地址后其他依赖 127.0.0.1:5555 的服务（如 dcgm-exporter）在测试期间连不上；`targetNumPairs=-1` 在 72 GPU 上的耗时（据此调 `time_to_run`）；workload 日志的实际文件名与位置。

### nvshmem：S7b NVSHMEM perftest（可选，默认关闭）

mpi 模式，镜像默认 `moe_bench`（需含 `libnvshmem3-dev-cuda-13`，perftest 在 `/usr/bin/nvshmem_13/perftest/device`）。rank 0 容器执行 `node_scripts/nvshmem_run.sh`：设置 `NVSHMEM_BOOTSTRAP=MPI`、`NVSHMEM_MACHINE_READABLE_OUTPUT=1`，逐个运行 `tests` 中的 perftest。`nvshmem.pair` 在 2 台主机上各用 1 张 GPU 测 put/get 带宽和延迟；`nvshmem.rack` 用整个 rack 的 GPU 测 alltoall 延迟。结果只记录（带宽取最大消息、延迟取最小消息），perftest 退出码非 0 时该组失败。

### nixl：NIXLBench（可选，默认关闭）

mpi 模式，镜像 `images.nixl_benchmark`（[`benchmarks/nixl-benchmark`](../nixl-benchmark/README.md)，GB300 用 `latest-cuda13-gb300`）。mtest 把 case 清单（`nixl_cases.tsv`）和主机配对（`nixl_pairs.tsv`）写到 `/mtest`；rank 0 容器执行 `node_scripts/nixl_run.sh`：在本容器启动 etcd（mgmt 地址，`etcd_port` 默认 12379），每个 case 先起 initiator、5 秒后起 target（两进程在 etcd 汇合，先注册的是 rank 0），其他主机上的进程经 ssh 在其容器内启动，并读取该容器的环境（`/proc/1/environ`），所以每台主机自己的 `UCX_NET_DEVICES` 生效。测试项与 [S1-S6 smoke 套件](../nixl-benchmark/nixlbench-acceptance-smoke-suite.md) 对应：

| 测试项 | case | 说明 |
|---|---|---|
| `nixl.node` | `s1`、`s2` | 单机 `gpus[0]` → `gpus[1]`（NVLink / cuda_ipc），`s2` READ + 一致性校验；`scope: node` |
| `nixl.pair` | `s3`–`s6` | 跨节点 RDMA：DRAM↔DRAM、DRAM→VRAM、VRAM↔VRAM（GPUDirect，一致性校验）、大块压测；`pairs: sequential`（`(h1,h2)(h3,h4)…`，奇数台时 h1 再与最后一台配对）或 `rack_adjacent` / `rack_half` / `cross_rack`；各对依次运行 |
| `nixl.storage` | `posix`、`posix_read`、`gds`、`gds_read` | 本机存储，`filepath` 为主机目录（挂到容器同一路径，运行前后删除测试文件）；读 case 读取前面写 case 的文件 |

判定：nixlbench 退出码非 0（`124` 为超时）、没有结果表、出现 `Consistency check failed` 时该次运行失败；`min_bw_gbps`（最大块带宽）、`max_lat_us`（最小块延迟）按 case 设置，不设只记录。应运行但没有结果的 case（脚本中途退出）记为 `not run`。跨节点失败时两端主机都标为故障主机（`group_result: false`），组内其余主机取组结果。报告显示每次运行的最小块延迟、最大块带宽和带宽曲线；单机 case 的带宽、延迟另写入主机指标 `nixl/<case>/bw_gbps`、`nixl/<case>/lat_us`。

### superbench：S6 SuperBench 与 RDMA 打流

每组在 rank 0 主机上启动 sbcli 镜像（`images.sbcli`，需与主机同为 arm64），由它对组内所有主机执行 `sb deploy` / `sb run`（脚本 `node_scripts/sb_run.sh`）。worker 镜像 `images.sb_worker` 须已在每台主机上（ISO 预置），预检会逐台检查。

- inventory：名称为主机的 mgmt 地址（MPI 走业务网），`ansible_host` 为 LOM 地址（SSH）。
- SSH 密钥：本次运行生成的密钥在预检时加入每台主机的 `authorized_keys`，结束后（包括失败、取消）删除，rank 0 主机上的私钥同时删除；不会把 `cluster.yaml` 中的密钥拷到主机。
- `benchmarks.include` / `exclude` 改写配置的 `superbench.enable`。
- `timeout`（默认 14400 秒）覆盖 `runtime.run_timeout`。
- 主机名：SuperBench 用各主机的短 OS 主机名（Ansible facts）命名节点和 ib-traffic 指标，mtest 预检时读每台主机的 `hostname`，结果映射回 `cluster.yaml` 中的主机，即使 OS 主机名与 `cluster.yaml` 不同也能对上。
- RDMA 配对（`ib_pairs`）：`rack_adjacent`（`superbench.rack` 默认：每个 rack 内 tray 1-2、3-4……，所有 rack 同一轮；奇数台时最后一台在第二轮与第一台配对）、`rack_half`（tray i 与 i+n/2）、`cross_rack`（`superbench.cross_rack` 默认：rack A 第 i 台与 rack B 第 i 台；多于 2 个 rack 时每种 rack 组合一轮，`cross_rack_rounds: first` 只跑第一轮）、`round_robin`（SuperBench 原生 one-to-one，所有节点对，n−1 轮）。前三种由 mtest 生成 ib-traffic 配对文件：先按 SuperBench hostfile 的顺序（OS 主机名不区分大小写排序）换算行号，写到每台主机的 `/mnt/mtest/<run>/<项>-<组>/ib-pairs.txt`（worker 容器挂载 `/mnt`），用 `--config-override ...ib-traffic*.parameters.config=` 传给 `ib-traffic`，结束后删除；配对表保存在 `raw/ib-pairs-hosts.tsv`。每对的 4 个 rail 同时打流（rail k ↔ rail k）。

| 测试项 | 配置 | 结果 |
|---|---|---|
| `single_node` | `config_gb300_single_node.yaml` | 所有主机一次执行，按主机拆分：每个 benchmark 的 `return_code` 为检查项，非 0 时该主机失败；每块 GPU 的指标取最差值；NCCL、ib-loopback 只在报告中显示最大消息的数值，其余写入 `metrics.csv` |
| `pair` / `rack` / `cross_rack` | `config_gb300_multi_node.yaml`，只启用 ib-traffic | 组结果：ib-traffic 按节点对、rail 解析；（主机, rail）离群 |
| `single_node` | `config_gb300_single_node.yaml`，排除 NCCL | 按主机拆分（同下） |
| `nccl_node` | `config_gb300_single_node.yaml`，只启用 NCCL（5 种操作） | 所有主机一次执行，按主机拆分；每台主机记录最大消息 busbw 与 8 B 时延 |
| `nvl_domain` | `config_gb300_nvl_domain.yaml` | 组结果：NCCL busbw 表（含小消息时延）与曲线、NCCL 网卡使用表；可设 `baseline` |
| `all` / `soak` | `config_gb300_all_nodes.yaml`（柜内 NVLink + 柜间 IB；`soak` 只选 `nccl-bw:soak:allreduce:mnnvl-ib`） | 同上 |

NCCL 网卡校验（`superbench.common.hca_check`，默认 `fail`）：从 SuperBench 保存的原始输出中读取 NCCL 日志（`NCCL_DEBUG=INFO`），按主机检查 `NET/IB : Using` 中只有 IB 设备、且（有 S3 结果时）都是 CX8 rail；不经 PXN 代理的 `Channel ... via NET/IB/<k>` 中，GPU d 用的是 GPU d 的 rail；没有 `NET/Socket`。不符的主机失败（`warn` 只告警，`off` 关闭）；没有 NCCL 日志时记为跳过。SuperBench 配置没有设置 `NCCL_IB_HCA`，NCCL 自行选择 RDMA 设备，BF3 的 RoCE 口可能被选中，这项检查用来发现这种情况。小消息时延取 `small_message_bytes`（默认 8 B、64 KiB）两端的 NCCL `time`（us，各 rank 取最差）。

报告的“RDMA 打流”页合并 `pair`、`rack`、`cross_rack`、`all` 中的 ib-traffic 结果（后一项的轮次接在前一项之后），画出每个 rail 的节点对矩阵（带宽、双向带宽、时延），失败的测量（-1）显示为黑格。

磁盘测试（`superbench.disk`）在 P10 实现。

### imex：S7 IMEX 配置与测试（每个 rack 一组）

1. 主机侧（`configure: true` 时）：把 rack 的 mgmt 地址列表（rank 顺序）以相同路径、相同内容写到每台主机，执行 `<scripts_dir>/imex-configure.sh --nodes <列表> --expect <台数>`；任一主机失败，整组不启动。
2. rank 0 主机等待 `nvidia-imex-ctl -N` 显示 `Domain State: UP`（`wait_ready_seconds`），超时则整组记为 error，不启动容器。
3. 每台主机执行 `imex-check.sh --quiet --level 3`：L0/L1 失败算到该主机；L2/L3 是整个 domain 的状态，只在组上显示。
4. mpi 模式启动 worker 容器（`images.sb_worker`），rank 0 执行 `node_scripts/imex_bench.sh`：T1 在每个容器内用 mpicc 编译 `imex_fabric_ipc_test.c`（链接自 `build-iso/scripts/`），先跑 2 节点再跑全部节点；T2（2 节点 multinode nvbandwidth）默认关闭，由 `nvlink.pairs` 覆盖整个 rack，`bench.t2_nvbandwidth: true` 可重新打开。

报告的组卡片显示连接矩阵、READY、Domain State、T1/T2 结果。柜内 MNNVL 集合通信由 `superbench.nvl_domain` 测试（在 `imex` 之后执行）。

### sharp：S7a IB SHARP（L4）

用 SuperBench 执行 `config_gb300_sharp.yaml`，每个操作（`ops`）、每组 NCCL 设置（`cases`）是一个 nccl-bw 条目 `nccl-bw:sharp:<op>:<case>`：

| 组 | 设置 | 用于 |
|---|---|---|
| O-A / O-B / O-C / O-D | 全关 / 柜内 NVLS（NVLSTree）/ 只开 IB SHARP（CollnetDirect）/ 两级 | `sharp.scale_out`，`groups` 指定跨 rack 组合 |

每组都显式设置 `NCCL_NVLS_ENABLE`；O-C、O-D 把 HPC-X 的 SHARP 插件与库加入 `LD_LIBRARY_PATH` 并开启 SAT。报告显示每组 busbw 曲线、最大消息 busbw、O-B/O-A、O-D/O-B、O-D/O-C、O-C/O-A 倍数（只展示，不判定），以及 NCCL 日志中 NVLS、CollNet/SHARP、插件、算法相关的行；某组返回码非 0 记为失败。需要新版 NCCL 时用 `sharp.common.worker_image` 指定加层镜像。柜内 NVLS 与关闭 NVLS 的对比由 `superbench.nvl_domain` 完成，配置中的 U-A、U-B 不再执行。

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
mtest run [item ...] [--hosts R1] [--exclude h1,h2] [--from item] [--force] [--no-gate]   # L0–L4
mtest status | logs <run-id> | cancel <run-id>
mtest report                                # S9
```

## 开发

```bash
make test-testkit       # 或 cd benchmarks/testkit && python3 -m unittest discover -s tests -v
benchmarks/testkit/e2e/run-e2e.sh [--keep]       # 执行器端到端测试，约 3 分钟
benchmarks/testkit/e2e/run-net-e2e.sh [--keep]   # S3/S4 端到端测试，约 4 分钟
```

端到端测试不需要 GPU 节点：在本机用 Docker 启动 3 个“假节点”（sshd + docker CLI，使用本机 Docker），再经真实 SSH 执行 `mtest`，覆盖 per-node、mpi、delegate 三种方式、单台失败、超时、`mtest cancel`、SIGTERM、`status`、`logs`、`report`，以及用官方 DCGM 镜像跑 `dcgm`（无 GPU，验证脚本下发、容器内 hostengine、原始输出拉回与解析、主机模式缺少 hostengine 时报错），和用 sb 替身镜像跑 `superbench`（替身经真实 SSH 用本次运行的密钥连每台主机并检查 worker 镜像，输出 SuperBench 格式结果；验证 include/exclude、按主机拆分、ib-traffic 节点对、运行后密钥删除、报告 RDMA 矩阵），以及 `imex`（主机上放替身 IMEX 脚本与 `nvidia-imex-ctl`；验证节点列表一致、等待 domain、矩阵解析，T1 经 mpirun 在各容器编译，无 CUDA 时报告编译失败，T2 跳过）、`superbench.nvl_domain`、`superbench.all`（小消息时延、NCCL 网卡使用表）与 `sharp.scale_out`（O-B/O-A 对比与 NCCL 日志提取），以及 imex 关卡停止与 `--no-gate`。需要本机有 Docker，首次运行会拉取 `docker:cli`、`debian:bookworm-slim` 并安装 OpenMPI。网段默认 172.28.0.0/24、172.29.0.0/24，冲突时用 `SSH_NET`、`MGMT_NET` 环境变量修改。

S3/S4 端到端测试启动 3 台 systemd 容器（netplan、systemd-networkd、sshd），LOM 由 netplan 静态配置（含默认路由，模拟 mtest 接入前已下发的状态），另有 mgmt、storage 和 4 个 rail 网络；容器内用假的 `/sys` 把 veth 映射成 BF3 与 CX8。主机由 `mtest import` 导入 CSV 规划表（含 PCI、网关、存储路由、`ib_mtu`）。覆盖：导入、识别与金样本比对、dry-run、按 PCI 下发（默认路由、LOM 策略路由与 LOM 默认路由移入策略表、BF3 MTU 不变、IB MTU、存储路由、跨 rail ping）、重新识别后重复执行无变更、错误网关触发自动回滚并恢复、`net verify`、规划表 PCI 在主机上不存在时拒绝、冲突条目拒绝下发、报告。需要能拉取 ubuntu:24.04（`UBUNTU_IMAGE` 可改为镜像站），网段默认 172.28.10–43.0/24（`NET_PREFIX` 可改）。

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
| P7 | S7 IMEX、S7a SHARP | 完成（实机待确认） |
| P8 | S8 MoE | 待做 |
| P9 | testkit 镜像、文档 | 待做 |
| P10 | SuperBench 磁盘测试（安全检查） | 待做 |
| P11 | 分层与关卡、rack 完整性、链路错误计数器与内核日志、L0 `links`（含 `mlxlink`）、RDMA 柜内/跨柜固定配对、S7b `nvlink.*`（mndiag）、离群与基线、`nvshmem.*` | 完成（单元测试与 e2e；实机待确认） |
| P12 | `nixl.*`（NIXLBench：单机、跨节点、存储） | 完成（单元测试；实机待确认） |
