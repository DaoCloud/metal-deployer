# GB300 批量装机、配置与测试验收计划

> 状态：设计稿，按里程碑逐步实现。本文定义整体流程、职责分工、输入格式、各阶段通过标准和里程碑。
>
> **当前实现方式**：Web UI、控制面服务（API、数据库、K8s 部署）和它的命令行客户端 `macc` 暂不实施，第 6 节只保留设计。S3–S9 由命令行工具 `testkit`（[benchmarks/testkit](../benchmarks/testkit/README.md)）在一台控制机上经 LOM SSH 执行：配置写在 `cluster.yaml`（主机、网络，规划表用 `testkit import` 导入）和 `tests.yaml`（测试项、主机范围、scope），结果拉回本地并生成 HTML 报告。下文的“控制面”在当前阶段即指 testkit 所在的控制机；涉及 UI、数据库、回调、`nico-sync` 的内容属于暂不实施的部分。

## 1. 目标与范围

对一批 GB300 NVL72 计算节点（2–4 个 rack，约 72 台 compute tray）完成从装机到测试验收的全流程，并集中查看结果（最初设计为一个 Web UI，当前阶段用 testkit 的命令行和 HTML 报告代替）：

1. 装机：NICo 经 LOM 口 PXE 安装本仓库构建的 ISO，首启自动安装驱动和工具。
2. 装机进度可见：每台主机处于哪个阶段、成功还是失败、失败日志。
3. 批量网络配置：BlueField-3（BF3）两个口配业务管理网和存储网；4 张 ConnectX-8（CX8）确保为 IB 模式，配 IPoIB 地址和 MTU。
4. 测试验收：DCGM 硬件诊断 → SuperBench → IMEX 配置与测试 → MoE 测试（HybridEP、DeepEP V1、[MoonEP](https://github.com/MoonshotAI/MoonEP)、DeepGEMM）。
5. 结果收集：测试全部由控制面以 Docker 容器方式在各节点启动，日志和结果回传控制面，汇总为一份验收报告（当前为 `testkit report` 生成的 HTML 和 CSV）。

不在范围内：修改 NICo 本身；交换机、UFM、NMX 的配置（只做校验并提示）。

## 2. 单台 compute tray 硬件与网络

| 部件 | 数量 | PCI ID | 用途 | 网络 |
|---|---|---|---|---|
| B300 GPU | 4 | — | 计算 | NVLink（NVL72 rack 内） |
| ConnectX-8 | 4 | `15b3:1023` | 计算网卡，rail N 对应 GPU N | IB 计算网（UFM 管理） |
| BlueField-3 | 1（2 个 PF） | `15b3:a2dc` | NIC 模式；port 0 业务管理，port 1 存储 | 业务管理网、存储网 |
| LOM | 1 | 非 `15b3` | NICo 装机；控制面 SSH（进度读取、配置下发、测试） | HostInband（NICo DHCP） |
| 主机 BMC | 1 | — | 电源、Redfish | OOB（NICo DHCP） |
| M.2 NVMe | 1 | — | 系统盘（OS） | — |
| E1.S NVMe | 4–8（热插拔） | — | 本地高速缓存（数据集、模型、checkpoint 暂存） | — |

以下 `15b3` 功能不是网卡，识别时忽略：`1024`/`197d`（CX8 PCIe Switch/Bridge）、`2100`（CX8 Data Direct Interface）、`c2d5`（BF3 SoC Management Interface）。PCI ID 来源：[pci.ids](https://pci-ids.ucw.cz/)。实际地址以第一台主机的 `nic-discover` 结果为准（见 S3）。

### 2.1 本地盘规划

公开资料中的配置：

| 来源 | 系统盘 | 数据盘 |
|---|---|---|
| [NVIDIA NVL72 AI Factory 参考架构](https://docs.nvidia.com/enterprise-reference-architectures/nvl72-ai-factory/latest/components.html) | 1 × M.2 NVMe，用于 OS | 4 × E1.S NVMe，“通常用作高速本地缓存” |
| [Lenovo GB300-NVL72](https://pubs.lenovo.com/gb300-nvl72/server_specifications_technical) | 1 × 2 TB PCIe 4.0 M.2（机内） | 最多 8 × 4 TB PCIe 5.0 E1.S（前置热插拔） |
| [HPE GB300 NVL72](https://buy.hpe.com/in/en/compute/rack-scale-system/nvidia-nvl-system/nvidia-gb300-nvl72-by-hpe/p/1014890105) | 1 × 1.92 TB M.2 | 8 × 3.84 TB E1.S |

规划：

| 盘 | 用途 | 由谁配置 | 验收阶段 |
|---|---|---|---|
| M.2 | 系统盘，autoinstall 安装 OS | ISO | S1 校验系统确实装在 M.2 上 |
| E1.S × N | 验收期间保持空盘；验收后组 RAID0（`mdadm`）+ XFS，挂载到 `/raid`（沿用 DGX 惯例），作为本地缓存 | 控制面（验收后执行，可选） | S6 磁盘测试逐块测试；测试通过后再组 RAID |

要点：

- 系统盘只有一块，没有 RAID1；系统盘故障需要重装，数据不应放在系统盘上。
- **M.2 容量小于 E1.S**（2 TB 对 4 TB，1.92 TB 对 3.84 TB）。GB300 autoinstall 原来的 `match: size: largest` 会选中一块 E1.S 作为系统盘，已改为 `size: smallest`，并在安装时检查（见下）。
- 磁盘测试对象就是全部 E1.S。按上面的规划，验收期间 E1.S 本来就应该是空盘，磁盘测试的误写风险主要来自系统盘选错，修正 autoinstall 后风险大幅降低。
- 先测试、后组 RAID：fio 会覆盖写入，组好 RAID 后再测试会破坏 `/raid` 上的数据。

**NICo 与系统盘**（依据 NICo 文档 [Image-Based Operating Systems](https://docs.nvidia.com/infra-controller/documentation/configuration-day-1/image-based-operating-systems) 和开源代码 [ncx-infra-controller-core](https://github.com/NVIDIA/ncx-infra-controller-core) 的 `pxe/common_files/disk_imaging.sh`、`crates/api-model/src/hardware_info.rs`、`docs/operations/tenant-lifecycle-cleanup.md`）：

| 事项 | NICo 的行为 | 对本项目的影响 |
|---|---|---|
| 系统盘由谁选 | 只有“镜像方式”的 OS（`imageUrl`）由 NICo 选盘写入；本项目用 iPXE 启动 ISO，系统盘由 ISO 的 autoinstall `storage.match` 决定，NICo 不参与 | 必须在 ISO 里配置正确 |
| 镜像方式的选盘选项 `boot_disk`/`imageDisk` | `smallest`：容量最小的整盘，同容量时优先有 EFI 分区的；`/dev/disk/by-id/...`：指定盘；也可写 `/dev/nvmeXnY` | NICo 文档建议：启动盘在全部机器上都是最小盘时用 `smallest`；by-id 在全部机器上一致时才用 by-id |
| 不设置时的默认值 | 按设备名顺序取第一块有 EFI 分区的盘，没有则取第一块 `/dev/nvme*` | 擦盘后没有 EFI 分区，会落到 `nvme0n1`，GB300 上不一定是 M.2；不能依赖默认值 |
| 硬件清单 | Scout 发现阶段（装 OS 之前）上报每块 NVMe 的 `model`、`serial`、`size_mb`、`pci_path`，可用 `nico-admin-cli machine hardware-info show --machine <id>` 或 API 查看 | **不知道机器型号也能拿到 M.2 的型号和 PCI 路径**，用于确定 `storage.match` 和 `disk_selector` |
| 回收擦盘 | 机器释放（tenant 回收）时，Scout 对所有 NVMe 做 secure erase（`nvme format -s2`），擦盘失败机器不会回到 Ready | E1.S 上的 `/raid` 数据在重装或回收时会被清空；验收开始时 E1.S 一般为空盘 |

**本项目的做法**

1. 首批机器纳管后，S0 的 `nico-sync` 从 NICo 读取每台机器的 NVMe 清单，按容量和 `pci_path` 分出 M.2 与 E1.S，在 UI 上显示，并检查全部机器是否一致（M.2 都是最小盘、E1.S 数量相同）。
2. ISO 的 `storage.match` 已改为 `size: smallest`，与 NICo 推荐做法相同。安装开始时 `early-commands` 运行 [select_root_disk.sh](../build-iso/scripts/select_root_disk.sh)：推算会选中哪块盘，选中 USB/可移动盘或最小容量有多块不同型号的盘时，在分区前中止安装。需要调整时，在 NICo OS 定义的内核参数加 `md.root_disk=model:<型号>` 或 `md.root_disk=id_path:<插槽路径>`（取自 NICo 硬件清单），不需要重做 ISO；参数说明见 [build-iso/readme.md](../build-iso/readme.md)。`smallest` 的前提：M.2 在每台机器上都严格小于 E1.S（容量相同时安装程序按设备排序选，不确定）；装机时没有插 U 盘或挂载 BMC 虚拟磁盘（容量为 0 的空虚拟盘会被安装程序忽略）。
3. S1 校验根文件系统确实在 M.2 上（按序列号对照 NICo 清单）。
4. S6 的 `disk_selector` 直接取 NICo 清单中的 E1.S 型号和容量。

## 3. 总体架构

下图是完整设计。当前阶段 `acceptance-controller` 不部署，由控制机上的 `testkit` 承担其中“经 LOM SSH”的部分（网络配置下发、启动测试、收集结果）；NICo API 只读调用、主机回调、UI 暂不实施。

```
                 ┌──────────────────── NICo 管理节点（Kubernetes） ────────────────────┐
                 │  NICo Core（DHCP / PXE / site-explorer / API）                       │
                 │  acceptance-controller（本项目新增，独立 Pod）                        │
                 │    FastAPI + SQLite(PVC) + Ansible(S4) + Docker runner + Vue UI      │
                 │    ├─ 只读调用 NICo API：机器状态、BMC/LOM 地址                       │
                 │    ├─ 经 LOM SSH：读取装机进度、下发网络配置、启动测试、收集结果      │
                 │    └─ 接收主机回调（可选）：autoinstall / run_stage 事件               │
                 └───────────────┬──────────────────────────────────┬───────────────────┘
                     LOM 网（HostInband）                       OOB 网（可选）
                     SSH（控制面 → 主机）                       Redfish（主机/DPU BMC）
                     HTTP 回调（主机 → 控制面，可选）
                 ┌───────────────┴──────────────────────────────────┴───────────────────┐
                 │  GB300 compute tray × 72（R1…R4，每 rack 18 台）                      │
                 └──────────────────────────────────────────────────────────────────────┘
```

### 3.1 职责分工

| 事项 | 负责方 | 说明 |
|---|---|---|
| 主机 BMC、LOM 口、DPU BMC 的 IP | NICo | DHCP 分配；需要固定时在 Expected Machine 用 `fixed_ip` |
| BF3 切换为 NIC 模式 | NICo | Expected Machine 设 `dpu_policy: nic`；NICo 经 DPU BMC Redfish 下发 `Mode.Set` 并整机断电重启 |
| PXE 装机 | NICo | 安装本仓库 ISO，autoinstall 照常执行 |
| 首启安装驱动和工具 | ISO | [init_system.sh](../build-iso/scripts/init_system.sh) 按阶段执行 |
| 装机进度 | ISO + 控制面 | ISO 中 `run_stage` 写 `state.json`；控制面经 LOM SSH 轮询读取；主机回调为可选增强 |
| BF3 两个口的 IP、MTU | 控制面 | netplan 静态配置 |
| CX8 IB 模式、IPoIB 地址、MTU | 控制面 | `mlxconfig` + netplan |
| IPoIB partition MTU | 网络侧（UFM） | 需设 `mtu=5`（4096），主机侧才能用 4092 |
| BF3 模式校验 | 控制面 | 只读校验；不一致时告警，交给 NICo 处理，不自行切换 |
| 测试执行与结果收集 | 控制面 | 以 Docker 容器在各节点运行测试，结果回传，见“测试执行方式”一节 |

### 3.2 网络连接方式

依据 GB300 逻辑子网规划和 NICo 开源代码（[ncx-infra-controller-core](https://github.com/NVIDIA/ncx-infra-controller-core)）确认：

| 网口 | 硬件 | 用途 | 装好后的路由 |
|---|---|---|---|
| LOM | 主板板载网卡（LAN on Motherboard，非 `15b3`） | NICo 装机（DHCP/PXE）；**控制面 SSH 到主机** | testkit 接入前已由 netplan 静态配置；S4 把它的默认路由移入策略路由表，只供 LOM 源地址的回包使用（见下） |
| BF3 port-0 | BlueField-3 第 1 个数据口 | 业务网（VRF INBAND） | **默认路由** |
| BF3 port-1 | BlueField-3 第 2 个数据口 | 存储网（VRF STORAGE） | 只路由存储网段 |
| CX8 × 4 | ConnectX-8 | IB 计算网（UFM 管理） | IPoIB，各 rail 一个网段 |

要点：

- **控制面始终经 LOM SSH 到主机**，装机后、网络配置中、测试期间都走这一条。网络侧已确认 OOB ACL 放开 NICo 节点到 LOM 网段的 SSH。这样 BF3 配错时控制面仍能连上主机修正。
- **NICo 本身不会主动连主机 OS。** NICo 只有主机侧进程主动连它（Scout 在发现阶段经 mTLS gRPC 连 `nico-api` 1079；主机经 `nico-pxe` 8080/80 取启动文件），对主机的控制只走 BMC。所以控制面经 LOM SSH 不与 NICo 冲突。
- **LOM 策略路由（必须）**：S4 把默认路由切到 BF3 port-0 后，控制面经 LOM 发来的 SSH 包，回包会按默认路由从 port-0 出去，连接中断。因此 S4 给 LOM 配策略路由：源地址为 LOM 地址的流量查独立路由表（默认 100，内有 LOM 网段与经 LOM 网关的默认路由），从 LOM 出去；不需要预先列出控制面网段，任何经 LOM 访问主机的网段都可用。LOM 静态 netplan 中原有的主表默认路由由 S4 改写为 `table: 100`（netplan 跨文件合并删不掉它）。策略路由写法是在 LOM 现有的 netplan id 下合并；不能另起一个匹配同一网卡的 id，否则 networkd 只取其中一份，配置会丢失（已在 QEMU 中验证）。
- **DNS**：port-0 在规划中声明了 `nameservers` 时加上 port-0 的 DNS；LOM 静态配置中的 DNS 不变。
- **备选方案**：见 3.3 节。

### 3.3 网络配置下发方案（待定）

S4 的 BF3（及 IPoIB）IP、MTU 由谁、在什么时候下发，目前有三种方案，尚未最终确定。共同目标：IP 规划只维护一份表，配置逻辑变更不需要重打 ISO。

**规划表的来源**：NICo 纳管阶段（装 OS 之前）Scout 已采集每台机器的网卡清单，REST `MachineNetworkInterface` 含 `macAddress`、`vendor`/`device`（PCI ID，可区分 BF3 `15b3:a2dc`、CX8 `15b3:1023`、LOM）、`path`（PCI 地址）、`numaNode`、`slot`；`MachineInfiniBandInterface` 含 CX8 的 `guid` 和 PCI 信息；CLI 为 `nico-admin-cli machine hardware-info show --machine <id>`。因此可由程序从 NICo 导出清单，生成规划表模板（SN、bmc_ip、lom_mac、mgmt_mac、mgmt_ip、mgmt_mtu、storage_mac、storage_ip、storage_mtu、ibN 的 PCI 地址与 IP……），人工填写 IP 和 MTU。

**NICo 能做什么、不能做什么**（依据 [ncx-infra-controller-core](https://github.com/NVIDIA/ncx-infra-controller-core) 代码）：

| NICo 资源 | 能否写 IP | 如何生效到主机 |
|---|---|---|
| ExpectedMachine `interfaces[].fixedIp` | 能 | 只能经 `nico-dhcp` 按 MAC 下发（含网关、DNS、NTP、MTU）；`fixedMask`/`fixedGateway` 仅为兼容字段，不生效；需在对应 VLAN 配 DHCP relay；每个网段都下发网关，不支持 option 121 |
| Instance `interfaces[].ipAddress` | 能 | 仅 DPU 模式；NIC 模式必须 `autoNetwork: true`，不能指定网卡 |
| `nico-pxe` 的 `network-config`（`${tenant-cloudinit-url}`） | — | 只来自 Instance `userData` 的 `network:` 段原样透传，否则为默认“全部网口 DHCP”；**NICo 不会把 IP 数据生成为静态 netplan** |
| `nico-pxe` 的 `/public` 静态目录 | — | 可放任意文件，但需打镜像经 `bootArtifactContainers` 注入并重启 `nico-pxe`，不按主机区分、不鉴权 |

结论：NICo 原生只有 DHCP 这一种分配方式；“规划表 → 静态 netplan”的转换需由本项目的程序完成，NICo 最多负责按主机存储和下发。

**候选方案**：

| 方案 | 做法 | ISO 改动 | 网络侧改动 | 状态 |
|---|---|---|---|---|
| A. 控制面经 LOM SSH 推送（当前写法，见 S4） | 控制面轮询到首启完成后，自动经 SSH 下发配置脚本和本机规划并执行 | 无 | 无（LOM SSH 已放开） | 默认方案 |
| B. NICo userData + 节点拉取 | 程序把规划表转为每台 netplan，写入 Instance `userData` 的 `network:`；节点首启经 LOM 拉 `network-config`，校验后应用（只允许匹配 BF3 MAC、自检失败回滚） | 一个通用的“拉取、校验、应用”步骤（安装阶段从 `/proc/cmdline` 的 `ds=nocloud-net;s=` 记录 NICo 地址），规划与格式变化不需改 ISO | 无（`nico-pxe` 端口已放行） | 已在 QEMU 验证（见下） |
| C. NICo DHCP | 规划表转为 ExpectedMachine `fixedIp` + HostInband 网段（含 MTU）；主机 BF3 开 DHCP | 一份通用 netplan：BF3 开 DHCP、存储口 `use-routes: false` 并加存储网段路由、业务口 metric 优先 | 业务网、存储网 VLAN 加 DHCP relay 到 `nico-dhcp` | 未验证；改 ExpectedMachine 对已纳管机器往往需重新纳管 |

不论哪种方案，CX8/IPoIB 都由控制面经 LOM SSH 配置（IPoIB 没有可稳定匹配的 MAC；首启时 UFM partition 可能未就绪），LOM 的策略路由都需要（见 3.2 节）。

**方案 B 的 QEMU 验证结论**（Ubuntu 24.04.4，按 NICo `ubuntu-autoinstall` 模板内核参数启动，本地 HTTP 模拟 `nico-pxe`）：

- 安装程序不请求 `network-config`，安装环境只给 LOM 配 DHCP；NICo 默认配置与写入 BF3 静态配置两种情况下，安装环境和装好的系统完全一致；装好的系统 cloud-init 数据源为 `None`，之后不再访问 NICo。即 `network-config` 不会被自动应用，必须由首启步骤显式拉取。
- 首启脚本原型通过 10 个用例：无 NICo 地址时跳过；NICo 默认配置识别并跳过；MAC 不在本机、指向 LOM、PCI ID 不符、NICo 不可达时拒绝且 netplan 不变；网关不通自动回滚；正常应用后 IP、MTU、路由正确；NICo 不在 LOM 网段时仍经 LOM 可达；重复执行结果一致；重启后保持。
- 验证中发现：LOM 上的路由必须写在 LOM 已有的 netplan id 下由 netplan 合并，另起一个匹配同一网卡的 id 会导致路由丢失。
- 未验证：方案 B 的变体“把整份 autoinstall（含 per-host `autoinstall.network`）放进 userData”，该变体会整体替换 ISO 自带的 `autoinstall.yaml`，且安装期间即切换默认路由，风险较大，暂不考虑。

## 4. 输入数据

两个文件：`hosts.csv` 放每台主机不同的数据；`site.yaml` 放全局参数。两者以 ConfigMap 挂载，UI 也支持上传和校验。

当前用 testkit 时的对应关系：`hosts.csv` 的内容进入 `cluster.yaml` 的 `hosts` 段（由 `testkit import` 从 IP 规划表导入）；`site.yaml` 的 SSH、网卡期望、网络参数在 `cluster.yaml` 的 `ssh`、`network` 段，镜像、测试参数在 `tests.yaml`。下文出现 `hosts.csv`、`site.yaml` 的地方按此对应。

### 4.1 hosts.csv

```csv
hostname,rack,tray,serial,bmc_ip,lom_ip,mgmt_ip,storage_ip,ib0_ip,ib1_ip,ib2_ip,ib3_ip,tags
gb300-r1-t01,R1,1,1234567890001,10.0.0.11,10.10.0.11,10.20.0.11/24,10.30.0.11/24,10.100.0.11/16,10.101.0.11/16,10.102.0.11/16,10.103.0.11/16,
gb300-r1-t02,R1,2,1234567890002,10.0.0.12,10.10.0.12,10.20.0.12/24,10.30.0.12/24,10.100.0.12/16,10.101.0.12/16,10.102.0.12/16,10.103.0.12/16,
gb300-r2-t01,R2,1,1234567890019,10.0.1.11,10.10.1.11,10.20.1.11/24,10.30.1.11/24,10.100.1.11/16,10.101.1.11/16,10.102.1.11/16,10.103.1.11/16,skip-moe
```

| 列 | 必填 | 来源 | 说明 |
|---|---|---|---|
| `hostname` | 是 | 规划 | 唯一主键 |
| `rack` | 是 | 规划 | 一个 rack = 一个 NVL72 = 一个 IMEX 域 |
| `tray` | 是 | 规划 | 1–18；决定 UI 排序和 IMEX `nodes_config.cfg` 的节点顺序 |
| `serial` | 建议 | 规划或 NICo | 与 NICo、`dmidecode`、回调事件交叉核对 |
| `bmc_ip` | 建议 | NICo | 只读；`nico-sync` 从 NICo 拉取并比对 |
| `lom_ip` | 是 | NICo | SSH 目标；`nico-sync` 从 NICo 拉取并比对 |
| `mgmt_ip` | 是 | 规划 | BF3 业务管理口，CIDR |
| `storage_ip` | 是 | 规划 | BF3 存储口，CIDR |
| `ib0_ip`–`ib3_ip` | 是 | 规划 | IPoIB 地址，`ibN` = rail N；每个 rail 一个子网 |
| `tags` | 否 | 规划 | `;` 分隔，例如 `skip-moe`、`golden` |

校验规则：`hostname`、所有 IP 全局唯一；每个 rack 正好 18 行（可在 `site.yaml` 调整）；`tray` 在 rack 内唯一；同一 rail 的 IB 地址在同一子网。

### 4.2 site.yaml

```yaml
ssh:
  user: root
  key_secret: acceptance-ssh-key   # Kubernetes Secret
  parallel: 24
control:
  report_url: ""                   # 可选：主机回调地址，需 LOM 网可达；为空时只靠 SSH 轮询，见 6.7 节
nico:
  api_url: https://<nico-api>
  token_secret: nico-readonly-token
  readonly: true
rack:
  trays_per_rack: 18
nic:
  lom:
    policy_table: 100              # LOM 源地址回包使用的策略路由表，见 3.2 节
  dpu:
    expected_mode: nic
    mgmt_port: 0
    storage_port: 1
    mgmt_gateway: 10.20.0.1
    mgmt_mtu: 1500
    storage_mtu: 9000
    dns: [10.20.0.53]
  compute:
    expected_count: 4
    link_type: ib
    expected_rate_gbps: 800
    ipoib_mtu: 4092
    pkey: 0x7fff
tests:
  dcgm_level: 3
  superbench:
    profile: gb300                               # 必须显式指定，控制面节点可能是 x86
    cases: [single-node, multi-node]             # nvl-domain 在 S7 执行
    disk_selector: {}                            # 测试盘匹配条件，GB300 为全部 E1.S，例如 {model: "<E1.S 型号>", size_gb: 3840}；为空则不跑磁盘测试
    disk_exclude: []                             # 永不测试的盘，按 /dev/disk/by-id 名称
    disk_expected_count: 8                       # 每台主机期望的可测试数据盘数量，按实际配置填写（4 或 8）
    disk_parallel: false                         # true 时同一主机多盘并发测试
    rule: rule_gb300.yaml
  moe_suites: [hybrid-ep, deepep, moonep, deepgemm]
  results_dir: /var/lib/metal-acceptance/runs   # 节点上的结果目录
  master_port_base: 29500                        # 每组 MASTER_PORT = base + 组序号
  mpi_ssh_port: 2222                             # MPI 模式容器内 sshd 端口
  rendezvous_timeout: 600                        # 秒；多节点等待全部加入的上限
  run_timeout: 7200                              # 秒；单组执行上限
images:                                          # tag 与 digest 固定，结果中记录实际 digest
  dcgm: <registry>/metal-deployer/dcgm-diag:4.7.1@sha256:<digest>
  sbcli: <registry>/metal-deployer/sbcli:<tag>@sha256:<digest>
  superbench_worker: superbench/main:cuda13.0@sha256:<digest>
  moe_bench: <registry>/metal-deployer/moe-bench:<tag>@sha256:<digest>
registry: ""                                     # 内网仓库；为空时由控制面推送镜像
```

## 5. 流程阶段

每个阶段可以按单台、单个 rack 或全部主机执行。每个动作都可重复执行：先检查当前状态，有差异才修改，最后再验证。前一阶段未通过的主机，默认不进入下一阶段（testkit 中由 `runtime.stop_on_failure` 控制，`testkit run --force` 可手动放行）。

| 阶段 | 名称 | 粒度 | 执行方 | 现有资产 | 需新增 |
|---|---|---|---|---|---|
| S0 | NICo 纳管与 PXE | 主机 | NICo | — | `nico-sync` 只读同步 |
| S1 | 系统安装（autoinstall） | 主机 | ISO | user-data | `reporting` webhook |
| S2 | 首启安装 | 主机 | ISO | `init_system.sh` `run_stage`、`state.json` | 控制面 SSH 轮询；阶段事件回调（暂不做） |
| S3 | 网卡识别与模式校验 | 主机 | 控制面 | — | `nic-discover`、`dpu-mode-check` |
| S4 | 网络配置 | 主机 | 控制面 | [setNicRdmaMode.sh](../build-iso/scripts/setNicRdmaMode.sh) | playbook |
| S5 | DCGM 硬件诊断 | 主机 | 控制面 | DCGM 4.7.1、`gpu_burn` | 执行与解析 |
| S6 | SuperBench | 主机/rack/全部 | 控制面 | sbcli 镜像、`config_gb300_*.yaml`、`rule_gb300.yaml` | 执行与解析 |
| S7 | IMEX 配置与测试 | rack | 控制面 | `imex-configure.sh` / `imex-check.sh` / `imex-bench.sh` | 编排 |
| S7b | NVLink Fabric 数据面测试 | rack | 控制面 | DCGM 4.7.1 及 multinode 包（主机，ISO 预装）、NVSHMEM perftest | `dcgmi mndiag` 编排；NVLoom 日志解析；NMX-T 采集；离群判定 |
| S7a | IB SHARP（柜间）；柜内 NVLS 由 L4 `superbench.nvl_domain` 覆盖 | 跨 rack | 控制面 | nccl-tests、HPC-X SHARP 插件、`config_gb300_sharp.yaml` | 对比矩阵、显式设置 `NCCL_NVLS_ENABLE`、可选 NCCL 2.31.x 测试镜像、日志判定 |
| S8 | MoE 测试 | 主机/rack/跨 rack | 控制面 | `moe-bench` 镜像 | 编排 |
| S9 | 验收报告 | 全部 | 控制面 | — | 报告生成 |

### S0 NICo 纳管与 PXE

- 动作：只读调用 NICo API，获取每台主机的状态、序列号、主机 BMC IP、LOM IP、DPU policy，以及 Scout 采集的硬件清单（NVMe 型号、序列号、容量、PCI 路径；GPU；网卡）。
- 通过：机器状态为 NICo 的就绪状态，DPU policy 为 `nic`。
- 产物：`nico-sync` 与 `hosts.csv` 的差异列表（地址不一致、缺少主机、多出主机）。

### S1 系统安装（autoinstall）

- 动作（可选）：在 [user-data](../build-iso/config/cloud-init/user-data) 的 autoinstall 段加 `reporting`（`type: webhook`），每个安装步骤推送事件；`error-commands` 推送失败事件和日志末尾。回调地址来自内核参数，见 6.7 节。安装程序运行期间不能 SSH，不配置回调时此阶段只能从 NICo 的 Instance 状态判断，装完后从目标系统 `/var/log/installer/` 补取日志。
- 主机识别：此阶段事件不带序列号，控制面按来源 IP 匹配 NICo 记录的 LOM IP。
- 通过：收到安装完成事件，主机进入首次启动；根文件系统所在的盘是 M.2（首启上报 `findmnt /` 和对应盘的型号、`id_path`，控制面比对），否则标记失败，提示检查 `storage.match`。

### S2 首启安装

- 动作：
  - 驱动和工具都在首次启动的 cloud-init `runcmd` 中由 [init_system.sh](../build-iso/scripts/init_system.sh) 按阶段安装；安装程序阶段（`late-commands`）只装平台内核。
  - 阶段顺序：`ssh_keys → prepare → apt_sources → base_packages → nouveau → offline_debs → doca_install → gpu_debs → rdma_modules → container_runtime → docker_images → cpu_performance → nvidia_setup → hpcx_install → gdrcopy_build → test_tools_build → gpu_burn`。`ssh_keys` 排第一，控制面公钥在首启开始几秒内就写入，安装最耗时的 DOCA、GPU 驱动阶段期间即可 SSH 读进度。
  - 状态文件 `/var/lib/metal-deployer/state.json`，这是节点上唯一的机器可读状态来源：
    - 启动时把全部阶段写为 `pending`，控制面可直接按 `success + skipped` 占比算进度。
    - 阶段状态：`pending`、`running`、`success`、`failed`、`skipped`（`reason` 写明原因，例如未检测到 GPU）。
    - 每次状态变化先写临时文件再 `mv` 替换，读不到半个文件。
    - 顶层字段：`schema`、`iso_version`、`hostname`、`boot_id`、`overall`（`running/success/failed`）、`exit_code`、`current_stage`、`started_at`、`updated_at`、`finished_at`；每个阶段有 `name`、`status`、`rc`、`started_at`、`finished_at`、`reason`、`log`。时间为 UTC。
    - 阶段外的步骤出错、脚本被 `INT`/`TERM` 中断时，退出钩子把 `overall` 写为 `failed`，正在运行的阶段写为 `failed`。
    - 写状态失败不影响安装。
  - **主路径：** 控制面定时（默认 30 秒）经 LOM SSH 读取 `state.json`，失败阶段再读 `/var/log/metal-deployer/<阶段>.log` 末尾。主机能 SSH 上之前（安装程序阶段、重启中、sshd 未起）显示为“等待连接”，这段时间以 NICo 的状态为准。`overall` 长时间停在 `running` 且 `updated_at` 不再变化（例如断电）时标记 `stale`；`boot_id` 变化说明主机中途重启过。
  - **回调（暂不做）：** 以后需要实时刷新时，可在配置了 `report_url` 时由 `run_stage` 在开始、完成、失败时 `curl` 回调，内容与 `state.json` 相同；回调设 3 秒超时、失败忽略，不得影响安装。
- 通过：`overall` 为 `success`，所有阶段为 `success` 或 `skipped`；结束时上报一次版本信息（内核、驱动、DOCA、CUDA、DCGM）。

### S3 网卡识别与模式校验（只读）

- `nic-discover`：遍历 `/sys/class/net/*/device`，记录 vendor/device、PCI 地址、ibdev、`link_layer`、NUMA、MAC/GUID；用 `nvidia-smi topo -m` 确定 GPU N ↔ CX8（rail N）；BF3 两个 PF 按 function 号排序。
- `dpu-mode-check`：主机上 `mlxconfig -d <BF3> q INTERNAL_CPU_OFFLOAD_ENGINE`，`DISABLED(1)` 为 NIC 模式；可选经 OOB 读 DPU BMC Redfish `/redfish/v1/Systems/Bluefield/Oem/Nvidia` 的 `Mode`。当前值与下次启动值不同时显示“切换待生效”。
- 金样本：第一台主机的结果人工确认后标记为 `golden`，其余主机与之比对。
- 通过：CX8 正好 4 张、BF3 正好 2 个 PF、BF3 为 NIC 模式、GPU ↔ rail 映射与金样本一致。

### S4 网络配置

由 `testkit net` 执行（实现与用法见 [testkit README](../benchmarks/testkit/README.md) 的“网卡识别与网络配置”一节）：检查差异、幂等，`testkit net apply --dry-run` 用于下发前预览 netplan diff，`testkit net verify` 只验证。下面是流程设计，实现中写 `/etc/netplan/90-static-mgmt-storage.yaml`（含 LOM 策略路由）与 `90-static-compute.yaml`，网卡按规划表的 PCI 地址定位后用永久 MAC 匹配。

1. CX8 模式：`mlxconfig -d <CX8> q LINK_TYPE_P1`；不是 `IB(1)` 则设置为 1，标记需重启。
2. 需要时重启：经 SSH 重启，等待主机从 LOM 回来；必要时经 Redfish 断电重启。
3. 写 netplan（`/etc/netplan/90-static-*.yaml`）：BF3 port 0 配 `mgmt_ip`、默认路由（`mgmt_gateway`）、DNS、`mgmt_mtu`；port 1 配 `storage_ip`、存储网段路由、`storage_mtu`；IPoIB 接口配 `ibN_ip`、`ipoib_mtu`；LOM 在现有 netplan id 下加策略路由，原默认路由移入策略表（见 3.2 节）。MTU 全部取自 `site.yaml`，按现场实际声明。按 MAC 匹配网卡，不依赖接口名；配置只允许匹配 BF3、CX8 的 MAC，匹配到 LOM 即拒绝。
4. 应用（防失联）：应用前先在主机上设定时回滚（`systemd-run --on-active=120` 恢复旧 netplan 并 `netplan apply`），然后 `netplan apply`；控制面在 120 秒内重新经 LOM SSH 连上并通过第 5 步检查后取消回滚，否则主机自动恢复旧配置。
5. 应用后验证：
   - LOM：控制面仍能 SSH；`ip route get <控制面地址> from <LOM 地址>` 走 LOM。
   - `ibstat`：4 个口 `State: Active`，速率与 `expected_rate_gbps` 一致。
   - `ip link`：各接口 MTU 与 `site.yaml` 声明一致。IPoIB MTU 上限由 UFM partition 决定（`mtu=4` 时为 2044，`mtu=5` 时为 4092）；声明值超过上限时报告原因，不强改。
   - 连通性：同网段 ping 网关和同 rack 另一台主机（业务管理、存储、每个 rail）；默认路由走 port 0。
- 通过：以上全部满足。

### 测试执行方式：统一用容器（S5–S8）

所有测试由控制面（testkit）发起，在 GPU 节点上以 Docker 容器运行，结果全部回传控制机，汇总到报告。主机上只保留驱动、DCGM hostengine、IMEX daemon 等必须常驻的服务和配置；S7b 的 `dcgmi mndiag` 例外，使用主机上的 DCGM（见 S7b）。

**执行方式**

- 控制面 Pod 内置 Docker CLI，用 `docker -H ssh://root@<lom_ip>` 直接操作各节点的 Docker（经 LOM 网 SSH，不需要在节点上开放 Docker 端口）。
- 单节点测试：每台主机启动一个容器，并发数受 `ssh.parallel` 限制。
- 多节点测试（rack 内、跨 rack、全部节点）：一条命令在所有参与节点同时启动容器，详见下方“多节点批量启动”。
- 容器统一参数：`--gpus all --network host --ipc host --privileged --ulimit memlock=-1`，结果目录挂载到 `/results`。
- 每次执行有唯一 `run-id`；容器命名为 `acc-<run-id>-<stage>`，结束后删除，便于重跑和清理残留。

**多节点批量启动**

在控制机上用一条命令，就能在选定的所有测试节点上同时启动容器，完成单机、rack 内、跨 rack 的测试。testkit 中每个测试项的 `scope`、主机范围（`hosts` / `groups`）和参数写在 `tests.yaml`；命令行只选择测试项，并可临时缩小或排除主机。

```bash
testkit run moe.single                       # 第 1 档：每台单机测试，各自出结果（scope: node）
testkit run moe.rack --hosts R1,R2,R3,R4     # 第 2 档：每个 rack 一组（18 节点、72 GPU，走 NVLink/MNNVL），各组依次执行
testkit run moe.cross_rack                   # 第 3 档：groups: [[R1, R2]]，rack 内走 NVLink，跨 rack 走 IB
testkit run moe.all                          # 第 4 档：全部节点一组（72 节点、288 GPU）
testkit run moe --exclude gb300-r1-t07       # 全部 enabled 的 moe 项，排除故障节点
testkit run                                  # 按内置顺序执行全部 enabled 项
```

`moe.rack` 跑哪个 suite（`hybrid-ep`、`deepep`、`moonep`）由 `tests.yaml` 中该项的 `args` 决定；要依次跑多个 suite 时修改 `args` 后分别执行。`testkit plan` 打印每组的节点、rank 和渲染后的命令，`testkit status`、`testkit logs`、`testkit cancel` 查看和停止执行。

分组规则（`tests.yaml` 中每项的 `scope`）：

| scope | 分组 | 默认并发 |
|---|---|---|
| `node` | 每台主机一组，`WORLD_SIZE=1` | 组间并行 |
| `rack` | 每个 rack 一组 | 组间串行（`runtime.group_order: serial`），避免多组共享 IB 计算网互相影响性能结果；可改为 `parallel` |
| `group` | 选中的全部节点组成一组（或在 `groups` 中列出多组） | 按 `groups` 依次执行 |

每一组的启动流程（设计；testkit 已实现其中预检、rank、注入、同时启动、监控、收集、清理，跨 run 的节点执行锁随控制面暂不实施）：

1. **加锁**：控制面在数据库中为本组全部节点加执行锁（一个节点同一时刻只属于一个 run），加锁失败则整组不启动并提示占用它的 run；run 结束、失败或取消时释放。
2. **预检**（并行）：SSH 可达；所需镜像存在且 digest 一致；没有其他测试容器在运行（防止锁之外的残留）；IB 4 个口 `Active`；测试需要 MNNVL 时，IMEX 状态正常。任一节点不通过，整组不启动，并列出原因。
3. **分配 rank**：按 `rack`、`tray` 排序，依次编号 `NODE_RANK=0…N-1`。同一 rack 的节点编号连续，HybridEP 才能正确识别 NVLink 域。rank 0 为主节点。
4. **注入变量**：每个容器统一注入下表变量。
5. **同时启动**：控制面并发执行 `docker run -d`，所有节点几乎同时启动；容器内进程在 rendezvous 处等待全部节点加入，超过 `rendezvous_timeout` 报错退出。
6. **监控**：跟随所有容器日志；任一容器非零退出时，立即停止本组其余容器，把这一组标记为失败，并记录最先失败的节点；超过 `run_timeout` 时也停止整组。
7. **收集**：拉回每个节点的 `/results`；以 rank 0 的 `summary.json` 作为组级结果（带宽、延迟等），各节点的结果作为节点级结果。
8. **清理**：删除本组容器，节点上的结果目录在拉回后按保留策略清理。释放执行锁。

注入的变量：

| 变量 | 值 |
|---|---|
| `RUN_ID`、`GROUP_ID` | 执行和分组标识 |
| `NODE_RANK`、`RANK` | 节点编号（moe-bench 用 `RANK` 表示节点编号） |
| `WORLD_SIZE`、`NNODES` | 组内节点数 |
| `GPUS_PER_NODE` | 4 |
| `MASTER_ADDR`、`MASTER_PORT` | rank 0 的业务管理网地址；端口取自 `site.yaml`，每组不同，避免冲突 |
| `NODE_LIST`、`RACK_LIST` | 按 rank 排序的节点 `mgmt_ip` 列表，及各节点所属 rack |
| `NCCL_SOCKET_IFNAME`、`GLOO_SOCKET_IFNAME` | 业务管理网接口名（来自 S3 网卡映射），控制流量走业务管理网 |
| `NCCL_IB_HCA`、`UCX_NET_DEVICES` | 4 张 CX8 的 ibdev（来自 S3 网卡映射），数据流量只走计算网，排除 BF3 |

支持三种启动模式，按镜像选用：

| 模式 | 做法 | 适用 |
|---|---|---|
| 逐节点启动 | 每个节点的容器各自运行同一入口，按 `RANK`/`MASTER_ADDR` 汇合 | moe-bench（HybridEP、DeepEP V1、MoonEP；MoonEP 由 `moe-bench` 内部调用 `torchrun`）、PyTorch 类测试 |
| MPI | 所有节点先启动带 sshd 的容器（端口取自 `site.yaml`）；控制面在 rank 0 容器内执行 `mpirun`，hostfile 由控制面生成 | nccl-tests、`nvbandwidth` multinode、IMEX T1–T3 |
| 委托 | 控制面只启动一个控制容器，由它自己分发到各节点 | SuperBench（sbcli） |

**结果回传**

1. 实时日志：`testkit logs <run-id> --follow` 经 SSH 跟随容器日志（完整设计中由控制面经 SSE 推送到 UI）。
2. 结果文件：容器写入节点 `runtime.node_results_dir`（默认 `/var/lib/testkit/runs`，挂载为 `/results`）；容器退出后拉回控制机的 `runtime.results_dir`，再解析。
3. 进度（可选，暂不实施）：容器内向 `report_url` POST 阶段事件，格式同 6.2 节。

**容器结果约定**（所有测试镜像都需遵守，现有镜像用包装脚本适配）

| 项 | 约定 |
|---|---|
| 退出码 | `0` 通过；`1` 测试不达标；`2` 执行出错（环境、参数、依赖问题） |
| `/results/summary.json` | 必须有：`stage`、`hostname`、`status`、`started_at`、`finished_at`、`image`（含 digest）、`metrics`（指标名 → 数值和单位）、`checks`（检查项 → pass/fail 和原因） |
| `/results/raw/` | 工具原始输出，原样保存 |
| `/results/env.json` | 驱动、CUDA、NCCL、固件等版本，便于对比不同主机 |

**镜像清单**

| 阶段 | 镜像 | 现状 | 需做的事 |
|---|---|---|---|
| S5 DCGM | 官方镜像 `nvcr.io/nvidia/cloud-native/dcgm:4.7.1-ubuntu24.04`（含 arm64 与 CUDA 13 诊断插件，版本与主机一致） | 已有 | 不另做镜像：testkit 下发包装脚本采集原始输出，在控制端解析；`hostengine: container`（默认，测试期间停主机 `nvidia-dcgm`，结束后恢复）或 `host` 两种模式均已实现，实机对比后选定 |
| S6 SuperBench | sbcli 控制镜像（含 GB300 配置和规则）+ `superbench/main:cuda13.0` worker | 已有 | sbcli 由控制面以容器启动，固定 `-p gb300`；把输出目录和 `rule_gb300.yaml` 判定结果转为每台主机的 `summary.json` |
| S6 磁盘 | 复用 `superbench/main:cuda13.0` worker 镜像（含 fio 3.28） | 已有 | 新增包装脚本：执行前复核、建立 `dataN` 逻辑名、`sb exec` 执行 `disk-benchmark`、输出 `summary.json` |
| S7 IMEX 测试 | 复用 SuperBench worker 镜像（MPI）：T1 `imex_fabric_ipc_test` 由 testkit 下发源码、在各节点容器内用 mpicc 编译后运行；T2 需要 MULTINODE 版 nvbandwidth，worker 镜像自带的版本没有 multinode 用例时跳过并注明 | 已实现 | IMEX 配置与 imex-check 在主机上执行；实机确认容器内 IMEX channel 可见、mpicc 与 CUDA 头文件可用 |
| S7a SHARP | 用 SuperBench 执行：`config_gb300_sharp.yaml`（nccl-bw，每组 NCCL 设置一个条目），worker 镜像 NCCL 2.27.7 + HPC-X 2.24.1 SHARP 插件 | 已实现 | 需要更新 NCCL 时可在 `sharp.common.worker_image` 指定加层镜像；实机确认插件路径 |
| S8 MoE | `moe-bench` | 已有（HybridEP、DeepEP V1、MoonEP、DeepGEMM 同一镜像） | 包装 `summary.json` |

**镜像分发**

- `site.yaml` 的 `images` 段固定每个镜像的 tag 和 digest；结果里记录实际使用的 digest。
- 节点上没有所需镜像时，按顺序处理：从内网仓库拉取；如果没有内网仓库，控制面执行 `docker save | docker -H ssh://... load` 推送。
- ISO 已预置的镜像（`load_docker_images.sh`）优先使用。

### 验收分层与关卡（S3–S8 的执行顺序）

验收有两个目标：（1）确认每一根线、每个链路正常，即基础硬件测试；（2）测通信库的 benchmark 和整机吞吐。两者按层执行：**先查链路，后测吞吐；每层通过才进入下一层**。任何一层失败就停在这一层排障，避免一口气跑完所有测试，最后分不清是哪根线的问题。每层除了测试本身，还要看硬件数据（链路状态、错误计数、光模块、内核日志），用来佐证问题出在哪根线、哪个端口。testkit 中的实现见 [testkit README](../benchmarks/testkit/README.md) 的“分层与关卡”。

| 层 | 测试项 | 范围 | 命令 | 佐证用的指标与硬件数据（除测试结果外） | 参考数据 |
|---|---|---|---|---|---|
| **L0 链路静态状态** | `links`（S3 `testkit nic discover` 先行） | 每台主机并行，只读，约 1 分钟 | `testkit nic discover`<br>`testkit run links` | `nvidia-smi nvlink -s`：每张 GPU 18 条链路是否 up、速率是否一致<br>`nvidia-smi nvlink -e`：错误计数现值<br>`nvidia-smi topo -p2p n`：节点内 GPU 两两 P2P<br>IB sysfs：端口 ACTIVE、速率、`symbol_error` 等计数现值<br>`mlxlink -m -c -e`：光模块型号与温度、raw/effective BER、effective physical errors<br>内核日志（本次开机以来）：`NVRM: Xid`、`fallen off the bus`、mlx5 错误、PCIe AER | NVLink5：18 条 × 50 GB/s，单向 900 GB/s<br>CX8：每口 800 Gb/s<br>effective physical errors = 0<br>BER、温度的门限按光模块规格或金样本设定 |
| **L1 单机** | `dcgm` → `superbench.single_node`（GEMM、显存、节点内 nvbandwidth、gpu-burn、CPU、ib-loopback；不含 NCCL） | 每台主机 | `testkit run dcgm superbench.single_node` | 测试前后对比：NVLink 和 IB 错误计数增量、链路掉线、新增的 GPU/网卡内核错误<br>`dcgmi diag`、`dcgmi health`、ECC、remapped rows、GPU Fabric 状态 | diag 全部 Pass；单机指标与全部主机中位数相差不超过 10% |
| **L2 RDMA**<br>阶段 A：柜内 | `superbench.rack` | 每个 rack：18 台相邻 tray 两两一对（1-2、3-4 ……），共 9 对，每对 4 个 rail（rail k ↔ rail k）同时打，一轮；每个 rack 36 组，2 个 rack 共 72 组 | `testkit run superbench.rack` | 前后错误计数增量（写明端口和 rail）、内核日志增量<br>每个（主机, rail）的带宽与组内中位数比较 | GPU Direct RDMA 单向、双向带宽应接近 800 Gb/s 线速；`ib_write_lat` 记录；具体门限按金样本定 |
| **L2 RDMA**<br>阶段 B：跨柜 | `superbench.cross_rack` | R1 第 i 台 ↔ R2 第 i 台，18 对，每对 4 个 rail，一轮；4 个 rack 时每种 rack 组合一轮，共 3 轮 | `testkit run superbench.cross_rack` | 同阶段 A；阶段 A、B 的对端不同：一台主机在两个阶段都离群，问题在这台主机或它的线；只在一个阶段离群，问题在对端或这条路径 | 与阶段 A 同 rail 的值应接近；明显偏低说明问题在 spine 或跨柜的线 |
| **L3 NVLink** | `imex` → `nvlink.pairs`（N1 每对 GPU 能否互访，N2 每对 GPU 的读写带宽）→ `nvlink.fabric`（NVLoom 并发、热点、多播、时延、20 分钟压力） | 每个 rack：72 GPU；N1 共 2556 对（一对一对依次测），N2 共 5112 个有向对 | `testkit run imex nvlink.pairs nvlink.fabric` | IMEX 连接矩阵、各节点 READY<br>前后 NVLink 错误计数增量（写明 GPU 和链路编号）、内核日志增量<br>N2 带宽矩阵和 NVLoom 结果中每张 GPU 的中位数<br>交换机端口计数（NMX-T，待做） | N1：2556/2556/0<br>N2：没有低于 rack 中位数 10% 的 GPU；有基线后不低于基线<br>NVLoom 6 个 suite 全部 Pass |
| **L4 NCCL** | `superbench.nccl_node`（单机 4 GPU，节点内 NVLink）→ `superbench.nvl_domain`（柜内 18 节点 72 GPU，只走 MNNVL，含 NVLS）→ `superbench.all`（全部 rack：柜内 NVLink + 柜间 IB，生产形态）→ `sharp.scale_out`（跨柜 IB SHARP：O-A~O-D）→ `nvshmem.*`（可选）→ `nixl.*`（可选）→ `superbench.soak`（跨柜 allreduce 长稳，默认关闭） | 每台主机 → 每个 rack → 全部节点 | `testkit run --from superbench.nccl_node` | NCCL 日志（`NCCL_DEBUG=INFO`）：每张 GPU 用自己 rail 的 CX8，没有用 BF3（RoCE），没有退回 NET/Socket；确认 MNNVL、NVLS、CollNet 生效<br>前后错误计数和内核日志：只告警 | 操作：allreduce、allgather、reducescatter、alltoall、broadcast；消息 1 B~16 GiB<br>`wrong=0`<br>记录最大消息 busbw 和 8 B、64 KiB 小消息时延<br>72 GPU allreduce（NVLS）：某第三方资料给出 GB200 为 750–830 GB/s，未核实，按金样本定 `baseline` |
| **L5 MoE** | `moe.single` → `moe.rack` → `moe.cross_rack` → `moe.all` | 第 1–4 档：单机、柜内、跨两柜、全部 | `testkit run moe` | 前后错误计数和内核日志：只告警 | 按金样本定基线，见 S8 |

**关卡**：L0–L3 的测试项默认是关卡（`gate: true`）。任何一项不是 pass，testkit 停止本次运行，打印失败的组、主机和原因，排障后用 `testkit run --from <项>` 接着跑。L4、L5 不设关卡；L4 不达标而 L0–L3 都通过时，按 NCCL 文档的思路优先排查通信库配置，而不是硬件。

**rack 完整性**：rack 级 NVLink 测试项要求整个 rack 都在。某台主机在前面失败被排除后，该 rack 记为 `blocked`，不会用 17 台照跑。

**RDMA 配对的实现**：SuperBench `ib-traffic` 支持自定义配对文件（`--config`，每行一轮，格式 `i,j;k,l`）。下标是 SuperBench hostfile 中的行号；这个 hostfile 由各主机的 OS 短主机名排序（不区分大小写）生成，与 `cluster.yaml` 的顺序无关。testkit 先读各主机的 `hostname` 再换算下标，把文件写到每台主机的 `/mnt/testkit/<run>/`（worker 容器挂载了 `/mnt`），通过配置覆盖传给 `ib-traffic`，测试结束后删除。原来的 `one-to-one`（每台与所有其他主机打流，n−1 轮）保留为 `ib_pairs: round_robin`。

### S5 DCGM 硬件诊断

- 动作：容器内执行 `dcgmi discovery -l`；`dcgmi health`；`dcgmi diag -r 3`（可选 `-r 4` 长测），日志参数使用 `--log-filename`（DCGM 4.7 要求）。补充检查：`nvidia-smi -q` 中 ECC、retired/remapped 行、NVLink 状态、Fabric `State: Completed`；`nvidia-smi topo -p2p n` 节点内 P2P 能力；可选 `gpu_burn`。
- 通过：diag 全部 Pass；4 张 GPU；无未纠正 ECC；NVLink 全部 up；节点内 4 张 GPU 两两 P2P 均为 `OK`。
- 产物：diag JSON、`nvidia-smi -q` 原文。

### S6 SuperBench

SuperBench 由两部分组成：sbcli 控制镜像（`sb` 命令、测试配置、汇总规则）和被测节点上的 worker 镜像（`superbench/main:cuda13.0`，ISO 预置，含 sm_103）。测试项和规模由配置文件决定。仓库已为 GB300 单独写了一组配置，worker 镜像不需要重建；改动和依据见 [superbench-gb300.md](./superbench-gb300.md)。

**GB300 专用配置**（`run-tests.sh -p gb300` 时选用，位于 `benchmarks/superbenchmark/image/configs/`）

| 配置 | `run-tests.sh -t` | 内容 | 节点范围 | 在本流程中的位置 |
|---|---|---|---|---|
| `config_gb300_single_node.yaml` | `single-node` | cublaslt-gemm（bf16/fp16/fp8，fp4 单列）、nvbandwidth（单节点用例）、gpu-copy-bw、gpu-burn（关闭 FP64）、cpu-stream × 4（Grace `neo2`）、ib-loopback（每颗 Grace 一组）、nccl-bw allreduce/allgather/reducescatter/alltoall/broadcast（节点内 NVLink） | 每节点独立运行，hosts 文件可含全部节点 | L1（`superbench.single_node`，不含 NCCL）；L4（`superbench.nccl_node`，只跑 NCCL） |
| `config_gb300_multi_node.yaml` | `multi-node` | ib-traffic GPU Direct RDMA 单向/双向 + cpu-to-cpu 基线、cpu-to-cpu 时延（2 字节，one-to-one 轮转覆盖所有节点对）、nccl-bw allreduce/allgather/reducescatter/alltoall（`NCCL_MNNVL_ENABLE=0`，同 rack 也走 IB） | 由 hosts 文件决定：2 节点 → rack → 跨 rack | L2（`superbench.rack` / `cross_rack`，只跑 ib-traffic，testkit 生成配对文件）；NCCL over IB 部分不在验收流程中 |
| `config_gb300_nvl_domain.yaml` | `nvl-domain` | nccl-bw allreduce/allgather/reducescatter/alltoall/broadcast 走 MNNVL（`NCCL_MNNVL_ENABLE=1`），allreduce 另有 NVLS 对照 | 必须是同一 rack（同一 NVLink 域）的节点 | L4（`superbench.nvl_domain`，依赖 IMEX） |
| `config_gb300_all_nodes.yaml` | （testkit 使用） | nccl-bw 5 种操作，柜内 NVLink + 柜间 IB（`NCCL_MNNVL_ENABLE=1`、`NCCL_NVLS_ENABLE=1`），另有 8 GiB allreduce 长稳用例 `nccl-bw:soak:allreduce:mnnvl-ib` | 全部 rack | L4（`superbench.all`、`superbench.soak`） |
| `config_gb300_disk.yaml` | （参数来源） | fio 磁盘测试，会覆盖写入设备 | 每节点独立 | S6 SB-1；由控制面在每台主机用 `sb exec` 执行，设备列表按主机分别注入，不经 `run-tests.sh --disk`；未声明测试盘时不跑 |
| `rule_gb300.yaml` | — | 汇总分类：GPU-COMPUTE、GPU-MEM、GPU-STABILITY、CPU-MEM、RDMA-LOCAL、RDMA-MULTINODE、NCCL-NVLINK、NCCL-MNNVL、NCCL-MNNVL-IB、NCCL-IB、DISK | — | 判定 |

与 x86 配置相比的主要差异：每节点 4 GPU（`proc_num: 4`）；GPU/NIC 按 Grace 分组亲和（`numa_dev: $((LOCAL_RANK/2))`）；只选 IB link-layer 设备，自动排除 BF3；移除 gemm-flops（测不到 SM103 峰值）、cpu-memory-bw-latency、cpu-hpl（无 arm64 实现）；新增 fp8/fp4 GEMM 和 MNNVL 测试。

**执行方式**（委托模式）

- testkit 在每组 rank 0 主机上以容器启动 sbcli，执行包装脚本 `sb_run.sh`：用 `tests.yaml` 中指定的 GB300 配置文件直接调用 `sb deploy` / `sb run`，由它们经 SSH 在各节点启动 worker 容器并用 `mpirun` 组织多节点测试。本次运行的临时 SSH 公钥先加入各节点 `authorized_keys`，结束后删除。
- 配置文件由 `tests.yaml` 显式指定，不经过 `run-tests.sh` 的 profile 选择。手工使用 `run-tests.sh` 时**必须显式传 `-p gb300`**：未指定 profile 时按 sbcli 所在机器架构选择，arm64 才默认 gb300，在 x86 控制机上会误用 x86 配置。
- inventory 由 testkit 按 rank 规则（`rack`、`tray` 排序）生成，只含通过前置条件的节点。
- 执行顺序与 scope 对应关系（scope 和主机范围在 `tests.yaml` 的 `superbench.*` 中设置）：

| 档 | 命令 | hosts 范围 | 说明 |
|---|---|---|---|
| SB-1 单机 | `testkit run superbench.single_node` | 全部就绪节点（一次 sbcli 调用，各节点独立跑） | 先跑，定位单机问题；声明了测试盘时，磁盘测试一并执行（见下） |
| SB-2 双节点 RDMA（可选） | `testkit run superbench.pair`（`hosts` 写 2 台） | 2 节点 | 冒烟，确认 GPU Direct RDMA 且未选到 BF3；默认关闭，阶段 A 已覆盖 |
| SB-3 RDMA 柜内（L2 阶段 A） | `testkit run superbench.rack` | 每个 rack：相邻 tray 两两一对，9 对 × 4 rail，一轮 | 只跑 ib-traffic（单向、双向带宽和时延） |
| SB-3b RDMA 跨柜（L2 阶段 B） | `testkit run superbench.cross_rack` | R1 第 i 台 ↔ R2 第 i 台，18 对 × 4 rail；4 个 rack 时 3 轮 | 只跑 ib-traffic |
| NCCL 单机（L4） | `testkit run superbench.nccl_node` | 每台主机 | 节点内 4 GPU，5 种操作 |
| NCCL 柜内（L4） | `testkit run superbench.nvl_domain` | 每个 rack | 只走 MNNVL，含 NVLS |
| NCCL 全部 rack（L4） | `testkit run superbench.all` | 全部节点 | 柜内 NVLink + 柜间 IB（生产形态） |
| NCCL 长稳（L4，默认关闭） | `testkit run superbench.soak` | 全部节点 | 8 GiB allreduce 重复，时长由配置中的 `iters` 决定 |

**磁盘测试并入 SB-1**

磁盘测试本身是单机测试，归在 SB-1 这一档，和 GPU、CPU 单机项一起出结果。它会对目标盘做顺序写和随机写，写错盘会破坏系统或数据，因此按以下方式执行。

*SuperBench 自带的磁盘测试*（`disk-benchmark`，源码 `superbench/benchmarks/micro_benchmarks/disk_performance.py`，与 worker 镜像同一提交 `9142bdf`）

- 本质是 fio（worker 镜像内置 fio 3.28）的封装：对 `block_devices` 列表中的每块盘依次执行 fio，固定参数 `--ioengine=libaio --direct=1 --output-format=json`；顺序测试块大小 128 KiB、随机测试 4 KiB；按 `seq/rand × read/write/readwrite` 组合，`*_runtime` 为 0 的项不跑（上游默认只开读测试，写测试默认关闭）；可选顺序或随机写预处理（默认关闭）。
- 指标：每块盘每项的 IOPS 和延迟 P95/P99/P99.9，指标名以设备文件名开头，例如 `nvme1n1_seq_read_read_iops`。
- **它不选盘**：只检查 `block_devices` 里每一项是块设备，不判断是否系统盘、是否有数据；列表为空时什么都不测。上游 Azure 配置直接写死 `/dev/nvme0n1`…`/dev/nvme7n1`（Azure VM 的系统盘不是 NVMe，所以可行）；本仓库 x86 配置写死 `/dev/nvme0n1`；GB300 配置 `block_devices` 为空，`run-tests.sh --disk` 把同一个设备列表注入给 hosts 文件里的所有主机。
- 可以按进程分盘并行：`proc_num` 设为盘数、`parallel: true`、`prefix: PROC_RANK={proc_rank}` 时，每个进程测列表中的第 `PROC_RANK` 块盘；默认 `proc_num: 1` 依次测试。
- `sb exec` 可以在单台主机上直接执行某个 benchmark（`sb run` 在各节点内部调用的就是它），配置和 `-C` 覆盖只作用于本机。

*为什么不能直接用 `run-tests.sh --disk`*

- 注入的设备名对同一次调用中的所有主机都相同，而 NVMe 设备编号在不同主机上、甚至同一主机重启前后都可能不同。
- 选盘和防误写完全靠调用方，SuperBench 不做任何检查。

*执行方式*：测试本身复用 SuperBench `disk-benchmark`（不新写测试工具，指标名和 `rule_gb300.yaml` 的 DISK 分类不变）；**选盘和安全检查由控制面新写**。控制面在每台主机启动一个 worker 容器（`superbench/main:cuda13.0`，`--privileged -v /dev:/dev`），容器入口是一个包装脚本：

1. 按已确认的序列号找到当前设备路径，并重新做下面“确认流程”第 2 步的全部检查；任何一项不符就退出，不执行 fio。
2. 在容器内建立逻辑名链接 `/run/metal-disks/data0`、`data1`…，分别指向这台主机已确认的 E1.S（按 PCI 地址排序）。指标名因此是 `data0_seq_read_...`，各主机一致，便于横向比较；`dataN` 与序列号的对应关系写入 `summary.json`。
3. 执行 `sb exec --config-file config_gb300_disk.yaml -C superbench.benchmarks.disk-benchmark.parameters.block_devices=[/run/metal-disks/data0,...]`。
4. 把 SuperBench 的结果转换为 `summary.json`，判定沿用 `rule_gb300.yaml` 的 DISK 分类门限。

各主机并行执行；同一主机内默认逐块测试（单盘结果不受其他盘干扰，8 块盘约 40 分钟）。需要测多盘并发总带宽时，另跑一轮 `parallel: true`。

说明：盘刚被 NICo secure erase 过，未做预处理时写性能偏高，属于新盘状态；要测稳态写性能需开启预处理（`enable_seq_precond`、`rand_precond_time`），耗时显著增加，默认不开。

*确认流程*

1. **只读盘点**（每台主机）：`lsblk -J -O` 记录每块盘的 `/dev/disk/by-id` 名称、型号、序列号、WWN、容量、挂载点、分区表、文件系统；`findmnt -no SOURCE /` 找出系统盘；`wipefs -n` 检查数据签名。结果入库。
2. **自动筛选**：按 `site.yaml` 的 `disk_selector`（型号、容量）匹配候选盘，并与 NICo 硬件清单中这台机器的 E1.S（型号、序列号）交叉核对；给每块盘打标签：`系统盘`（根文件系统所在盘）、`已挂载`、`被占用`（属于 md RAID、LVM 等，例如验收后建立的 `/raid`）、`有分区或签名`、`排除列表`、`可测试`。只有 `可测试` 的盘才能被选中；可测试盘数量与 `site.yaml` 的期望数量不一致时，整台主机需要人工确认。
3. **金样本确认**：UI 先展示金样本主机的全部盘及标签，操作人核对后选定测试盘，输入将被覆盖的盘数确认；先在金样本上执行，检查结果正常。
4. **批量确认**：UI 按 rack 汇总“每台主机将被覆盖的盘（型号、容量、序列号）”，并高亮与金样本不一致的主机，例如可测试盘数量不同、系统盘型号与测试盘相同、存在分区或签名。不一致的主机默认不选中，需要逐台单独确认。
5. **确认绑定序列号**：确认记录的是盘的序列号/WWN，不是 `/dev/nvmeXnY`；同时记录操作人和时间，写入报告。
6. **执行前复核**：由上面的容器包装脚本完成；任何一项不符就中止该主机，磁盘项标记为 `failed`，并说明原因。
7. 检查不通过的主机仍跑 GPU/CPU 单机项，磁盘项标记为 `skipped` 并写明原因。

未声明 `disk_selector` 时不跑磁盘测试，SB-1 行为不变。

*仍然存在的风险*

| 风险 | 说明 | 处理 |
|---|---|---|
| 盘上有数据但没有可识别的签名 | 裸盘直接写数据（无分区、无文件系统）时，`wipefs` 检测不到 | 磁盘测试只允许在“验收中”的主机上执行；主机交付后在 UI 中锁定，不能再跑磁盘测试 |
| 系统盘选择不确定 | `size: largest` 可能把系统装在某块数据盘上，各主机不一致 | 执行前复核会挡住系统盘；根本解决是让 autoinstall 固定装到启动盘（需单独修改 ISO 配置，见风险表） |
| 人工确认失误 | 批量确认时看漏 | 金样本先行；与金样本不一致的主机默认不选中；确认绑定序列号 |
| 确认后设备编号变化 | 重启或热插拔后 `/dev/nvmeXnY` 改变 | 绑定序列号，执行前复核，fio 使用 by-id 路径 |

- 结果回传：sbcli 输出目录（各节点 `results-summary.jsonl`、`sb result summary` 报告）整体拉回控制面；控制面按节点拆分，转换为每台主机的 `summary.json`，多节点项另存组级结果。

**通过标准**

- 按 `rule_gb300.yaml` 各分类判定；不达标的主机和指标在 UI 标红。
- 门限：实测前用 OEM 同型号交付指标；首批金样本 rack 实测后更新为基线。

**前置**

- S5 DCGM 通过。
- 在金样本主机上完成 [superbench-gb300.md](./superbench-gb300.md) 第 6 节的实机确认项（GPU/NIC/Grace 亲和、IB 设备顺序、Grace 核编号、`nvidia-peermem`、fp4 GEMM、nccl-tests 在 SM103 上可运行、NCCL 未选到 BF3）。不一致时先调整配置，再批量执行。

**不在 SuperBench 中的测试**：多节点 nvbandwidth、IMEX fabric IPC、MNNVL 开关对比由 S7 `imex-bench.sh` 负责；HybridEP、DeepEP V1、MoonEP、DeepGEMM 由 S8 moe-bench 负责；IMEX 状态检查在主机上执行；stress-ng 长稳、Grace HPL 暂不纳入。

### S7 IMEX 配置与测试（按 rack）

- 配置（主机上执行）：控制面按 `rack` + `tray` 顺序生成该 rack 的节点列表（mgmt 地址），以相同路径、相同内容写到每台主机，调用 `imex-configure.sh`，再在 rank 0 主机上等待 `nvidia-imex-ctl -N` 显示 `Domain State: UP`，每台主机执行 `imex-check.sh`。详见 [imex.md](./imex.md)。
- 测试（容器内执行，testkit 测试项 `imex`）：MPI 模式，rack 内全部节点启动 worker 容器，由 rank 0 执行 T1（`imex_fabric_ipc_test`，2 节点与全部节点）。原 T2（2 节点多节点 nvbandwidth，worker 镜像没有 MULTINODE 版本时实际被跳过）取消，改由 S7b 的 DCGM `mnnvbandwidth` 覆盖整个 rack。
- 柜内 MNNVL 集合通信（原 T3）改用 SuperBench：测试项 `superbench.nvl_domain`（`config_gb300_nvl_domain.yaml`），在 S7b 之后执行。执行顺序：L3 `imex` → `nvlink.pairs` → `nvlink.fabric`，L4 `superbench.nvl_domain` → `superbench.all` → S7a `sharp.scale_out`，即先确认控制面，再测 GPU 点对点和 Fabric 并发，最后测集合通信。
- 通过：`nvidia-imex-ctl -N` 全部 READY，矩阵全 `C`，`Domain State: UP`；T1 通过；`superbench.nvl_domain` 各项返回码为 0，且 NCCL 日志确认 MNNVL 生效（同一 rack 的 rank 之间用 P2P/NVLS，没有 `NET/IB` 连接），`mnnvl-nvls` 一项确认 NVLS 资源分配成功（判定方法同 S7a）。imex-check 的 L0/L1 失败算到对应主机，L2/L3 是整个 domain 的状态，记在组上。
- 说明：Probe 未 Complete 属交换机侧（NMX/线缆）问题，报告中单独归类。

### S7b NVLink Fabric 数据面测试（按 rack）

S7 只证明 IMEX 控制面正常、两节点能互访；S7b 在整个 rack（18 节点、72 GPU）上测 GPU 之间的数据面：每对 GPU 能否互访、点对点带宽、多 GPU 并发下 Fabric 的表现，同时从交换机侧采集端口状态和错误计数。testkit 测试项 `nvlink.pairs`（N1、N2，L2 层，在 RDMA 点对点和 `imex` 之后）和 `nvlink.fabric`（N3，L3 层），N4 为 `nvshmem.pair` / `nvshmem.rack`（L4，可选）。分层和关卡见下文“验收分层与关卡”。

依据：[MNNVL User Guide – Verifying](https://docs.nvidia.com/multi-node-nvlink-systems/mnnvl-user-guide/verifying.html)、[MNNVL Multinode Testing](https://docs.nvidia.com/multi-node-nvlink-systems/mnnvl-user-guide/testing.html)、[DCGM mnnvbandwidth](https://docs.nvidia.com/datacenter/dcgm/latest/reference/multi-node-diagnostics/mnnvbandwidth.html)、[DCGM nvloom](https://docs.nvidia.com/datacenter/dcgm/latest/reference/multi-node-diagnostics/nvloom.html)、[dcgmi mndiag](https://docs.nvidia.com/datacenter/dcgm/latest/reference/command-line-reference/dcgmi/dcgmi-mndiag.html)、[NCCL Performance and Tuning](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/performance_and_tuning.html)、[NMX-T Prometheus Endpoint](https://networking-docs.nvidia.com/nmxtswum/123/prometheus-metrics-endpoint)。

**工具分工**

| 编号 | 工具 | 回答的问题 | 默认 |
|---|---|---|---|
| N1 | `dcgmi mndiag --run mnnvbandwidth`，`gpuPairs=all` | 72 GPU 的 2556 个无序 GPU 对能否互访，哪一对失败 | 开 |
| N2 | `dcgmi mndiag --run mnnvbandwidth`（正常模式） | GPU 之间 Copy Engine 读、写方向的带宽矩阵 | 开 |
| N3 | `dcgmi mndiag --run nvloom` | 多 GPU 并发、热点接收、Multicast、延迟、持续压力下 Fabric 的表现 | 开 |
| N4 | NVSHMEM perftest | GPU 发起的 Put/Get 带宽和延迟、alltoall 延迟 | 关（可选） |
| N5 | NMX-T 采集 | N1–N4 期间交换机端口状态、错误计数和吞吐 | 待做（testkit 尚未实现） |

NVBandwidth 和 NVLoom 不是二选一：前者测 GPU 对之间的传输能力，后者测复杂流量下整个 Fabric 的表现。NCCL 集合通信由 L4 的 `superbench.nvl_domain`、`superbench.all` 和 S7a 负责；NCCL 文档的排查顺序是先确认 NVBandwidth/NVLoom 正常，再查 NCCL 配置，所以 S7b 放在它们前面。NVSHMEM 不是 Scale-Up 基础验收的必选项，默认关闭；要上线依赖 NVSHMEM 的业务（如 DeepEP V1）时再打开，S8 的 `deepep` 也间接覆盖。DCGM 的 `mnubergemm`（计算与通信混合负载）暂不纳入。

**N1 全 GPU 对可达性**

```bash
dcgmi mndiag --hostfile hosts.txt --run mnnvbandwidth \
    --parameters 'mnnvbandwidth.gpuPairs=all;mnnvbandwidth.time_to_run=<秒>'
```

- 默认用例 `multinode_device_to_device_memcpy_read_sm`，1 MiB buffer，GPU 对依次执行；同节点和跨节点的 GPU 对都包含。72 GPU 共 72 × 71 / 2 = 2556 对。
- `time_to_run` 是整批的总预算，不是每对的预算；超时会中止整批且没有汇总结果。预算按金样本 rack 实测时间加余量确定。
- `Pairs Planned` / `Pairs Passed` / `Pairs Failed` 和日志路径只出现在文本输出中，JSON 输出里没有，因此按文本模式执行并解析；失败的 GPU 对最多列出 32 条（超过时前 31 条加 1 条省略计数）。
- 1 MiB、一次读只能证明能互访，不能证明带宽达标。

**N2 点对点带宽矩阵**

```bash
dcgmi mndiag --hostfile hosts.txt --run mnnvbandwidth \
    --parameters 'mnnvbandwidth.targetNumPairs=-1;mnnvbandwidth.time_to_run=<秒>'
```

- 默认用例 `multinode_device_to_device_memcpy_write_ce` 和 `multinode_device_to_device_memcpy_read_ce`，输出 Write、Read 两个带宽矩阵。
- 总 GPU 数大于 16 时，DCGM 默认只抽样与 GPU 数相同的对数（72 GPU 只测 72 对）。验收时设 `targetNumPairs=-1` 测全部 5112 个有向对；金样本上实测耗时过长时，改为抽样并在报告中注明对数。
- 带宽数值在 DCGM 捕获的 workload 日志中（默认 `/var/log/nvidia-dcgm`，由 `DCGM_HOME_DIR` 控制），控制面从日志解析成矩阵。

**N3 NVLoom Fabric 并发测试**

```bash
dcgmi mndiag --hostfile hosts.txt --run nvloom \
    --parameters 'nvloom.suite=<suite>;nvloom.time_to_run=<秒>'
```

- 主机 CUDA 驱动 API 为 13，DCGM 选用 NVLoom 2.0.0。单 rack 默认依次跑下表的 suite；`rack-to-rack` 需要多个 rack，`egm*` 与 GPU Fabric 验收无关，默认不跑。

| suite | 测什么 | 看什么 | `time_to_run` 默认 |
|---|---|---|---|
| `gpu-to-rack`（DCGM 默认） | 每张 GPU 与 rack 内其他 GPU 的抽样传输 | 带宽统计 | 金样本实测确定 |
| `pairwise` | 多组 GPU 对并发传输；加 `nvloom.rich_output=` 输出每个 GPU 对的测量值 | 带宽分布，低速 GPU 对 | 同上 |
| `all-to-one` | 多个 GPU 同时向一个 GPU 传输 | 热点接收带宽 | 同上 |
| `multicast` | NVLink 多播 | 能否完成，多播带宽 | 同上 |
| `latency` | GPU 之间的访问延迟 | 延迟统计 | 同上 |
| `fabric-stress` | 持续多 GPU 并发流量 | 吞吐、稳定性、错误 | 1200（20 分钟，与官方示例相同，不代表官方规定的老化时长） |

- DCGM 把 `time_to_run` 扣除 30 秒后按 suite 内用例数平分；不同 suite 的流量模型和统计方法不同，结果不能相加当作 rack 总带宽。
- DCGM 的文本和 JSON 输出只给 Pass/Fail/Skip、参与的主机与 GPU、错误信息，不含带宽和延迟；数值从 workload 日志解析。

**N4 NVSHMEM perftest（可选）**

- 二进制来自 NVIDIA 仓库的 `libnvshmem3-dev-cuda-13`（与 moe-bench 镜像一致用 3.4.5），位于 `/usr/bin/nvshmem_13/perftest/`；moe-bench 镜像里的 `nvidia-nvshmem-cu13` wheel 不含 perftest，需要在镜像中加装这个 deb。
- 用例：`device/pt-to-pt/shmem_put_bw`、`shmem_get_bw`、`shmem_put_latency`、`shmem_p_latency`，在跨 tray 的 2 节点上运行；`device/coll/alltoall_latency` 在整个 rack 上运行。MPI bootstrap（`nvshmem_bootstrap_mpi.so`）需与容器内 Open MPI 版本匹配，启动参数在金样本上确认。
- 只记录结果，不判定。

**NIXL NIXLBench（可选，L4）**

- 不属于 Scale-Up 基础验收；要上线 P/D 分离推理（KV cache 经 NIXL 传输，如 Dynamo、SGLang/vLLM 的 NIXL connector）时打开。镜像 `nixl-benchmark:<version>-cuda13-gb300`（[benchmarks/nixl-benchmark](../benchmarks/nixl-benchmark/README.md)，NIXL v1.5.2，UCX / LIBFABRIC / GDS / POSIX 插件）。
- testkit 测试项：`nixl.node`（单机 GPU↔GPU，S1/S2）、`nixl.pair`（跨节点 DRAM/VRAM RDMA 与 GPUDirect，S3–S6，可按 `rack_adjacent` / `cross_rack` 配对）、`nixl.storage`（POSIX / GDS）。用例与判定见 [nixlbench-acceptance-smoke-suite.md](../benchmarks/nixl-benchmark/nixlbench-acceptance-smoke-suite.md)。
- 一致性校验失败、进程异常即失败；带宽、延迟先只记录，金样本实测后填 `min_bw_gbps` / `max_lat_us`。

**N5 NMX-T 交换机侧采集**

- 计算节点上的 `nvidia-smi` 看不到交换机端口。NMX-T 在交换托盘上提供 HTTP 端点（如 `http://<NMX-T 地址>:9352/xcset/nvlink_domain_telemetry`），支持 Prometheus、CSV、JSON 格式，只返回最新一次采样。
- 做法：N1–N4 每一步开始前和结束后各取一次快照，比较端口状态和错误计数的变化；`fabric-stress` 期间按固定间隔轮询，记录各端口吞吐。只读，不修改交换机配置（与第 1 节范围一致）。
- `site.yaml` 增加每个 rack 的 NMX-T 地址和认证信息。需要采集的字段（端口状态、各类错误计数、收发字节数）按现场 NMX-T 版本的 fieldset 确认。
- NMX-C（GFM、NVLSM）的状态仍按 [imex.md](./imex.md) 由 fabric probe 和 `imex-check` 间接确认；NMX-T 不可用时 N5 记为 SKIP，不阻塞其他步骤，报告中注明缺少交换机侧数据。

**执行方式**（主机模式，不用容器；实现见 [testkit README](../benchmarks/testkit/README.md) 的 nvlink 一节）

`dcgmi mndiag` 由 head 节点的 nv-hostengine 用 Open MPI 经 SSH 在各节点启动 workload，与“测试统一用容器”的约定不同，原因：mndiag 需要每个节点的 nv-hostengine 互相可达，并由 head 节点以 root 身份 SSH 到其他节点；ISO 已安装同版本 DCGM 4.7.1，直接使用主机的 `nvidia-dcgm` 服务最简单。S5 在容器里运行 hostengine 时会停掉主机服务，S7b 开始前需确认主机服务已恢复。

1. ISO 预装 `datacenter-gpu-manager-4-multinode` 和 `datacenter-gpu-manager-4-multinode-cuda13`（`cuda13-gb300` 为 4.7.1-2，`cuda13-gb300-aggressive` 为 4.8.0-1；后者包含 `plugins/cuda13/` 下的 `mnnvbandwidth`、`nvloom_cli`、`mnubergemm`）。依赖的 `datacenter-gpu-manager-4-cuda13-{cublas,curand,cusparselt,nccl}` 已在 DCGM 包组中，Open MPI（`openmpi-bin`）由首启 base_packages 安装。multinode 包的 systemd generator 会给 `nvidia-dcgm.service` 设置 `DCGM_MNDIAG_MPIRUN_PATH`（Ubuntu 上优先 `/usr/mpi/gcc/openmpi-4.1.7rc1`，否则 `/usr/bin/mpirun`）。
2. 每个节点的 nv-hostengine 在管理网地址上监听 TCP 5555，供 head 节点连接。默认监听地址需在金样本上确认；若只监听 127.0.0.1，测试期间加 drop-in 改为管理网地址，测试结束后恢复。该端口没有认证，只允许管理网访问。
3. head 节点为 rack 的 rank 0。控制面以 root 执行 `dcgmi`，因此 head 节点上需在 `nvidia-dcgm.service` 中设置 `DCGM_MPIRUN_ALLOW_RUN_AS_ROOT`，并让 head 节点的 root 能免密 SSH 到 rack 内其他节点：控制面为本次运行生成临时密钥，私钥放到 head 节点、公钥加入其他节点的 `authorized_keys`，结束后删除（与 SuperBench 委托模式相同）。
4. `hosts.txt` 为该 rack 的管理网地址，按 rank 顺序。MPI 网卡由 DCGM 自动检测；检测结果不对时设 `openmpi.detect_interfaces=0`，并在 nv-hostengine 环境中设 `OMPI_MCA_*_if_include` 为管理网网卡。
5. 每个节点的 GPU 不能有其他进程（其他测试容器、dcgm-exporter 等），否则返回 `DCGM_ST_IN_USE` 或启动失败。

**判定**

DCGM 的 `Pass` 只表示传输完成、没有报错，不检查最低带宽；`Skip` 不算通过。NVIDIA 公开资料没有适用于所有 GB300 NVL72 的统一带宽或延迟门限，因此性能先做 rack 内离群检测，绝对门限在金样本 rack 实测后写入 `site.yaml` 的基线。

| 项 | 通过条件 | 失败归属 |
|---|---|---|
| N1 | `Pairs Planned` = N(N−1)/2（72 GPU 为 2556），`Pairs Failed` = 0 | 失败 GPU 对的两端都记失败；同一张 GPU 出现在多个失败对中时，优先怀疑这张 GPU 或它的链路 |
| N2 | DCGM 结果为 `Pass`；每张 GPU 作为源（行）和目的（列）的带宽中位数低于 rack 中位数 × (1 − `outlier_pct`) 时为离群 GPU；低于 `baseline` 的 GPU 对失败 | 离群 GPU 所在主机 |
| N3 | 每个 suite 为 `Pass`；nvloom_cli 每个测试用例按行（GPU）比较中位数，带宽低于、延迟高于 rack 中位数 `outlier_pct` 为离群 GPU | DCGM 报告失败的 GPU、离群 GPU 所在主机 |
| N4 | 只记录 | — |
| N5 | 测试期间端口均为 Active，错误计数没有增加 | 交换机端口或线缆（交换机侧问题，单独归类） |

`outlier_pct` 默认 10%，`outlier_action` 默认 `fail`：离群 GPU 所在主机记为失败，`nvlink.*` 是关卡项，run 随即停止，先排障再继续。改为 `warn` 时只在报告中列出，不阻塞。单条 NVLink 掉线只少约 5.6% 带宽，可能在 10% 以内，这类问题由 L0 的 `nvlink_up` 和链路错误计数器发现，不依赖离群判定。

**产物**：每个 rack 的 `dcgmi` 文本和 JSON 输出、DCGM workload 日志、Read/Write 带宽矩阵（CSV）、NVLoom 各 suite 的统计、NMX-T 快照与差值、`summary.json`。

### S7a NVLink SHARP 与 IB SHARP

依据：飞书文档《使用 nccl-tests 测试 NVLink SHARP 与 IB SHARP（GB300 Scale-up / Scale-out）》v1（2026-11-06）。用 nccl-tests 加 NCCL 环境变量即可，不需要专用工具；每项都做**开启 / 关闭 SHARP 的对比**，看 busbw 的差异。

| 名词 | 层级 | 含义 |
|---|---|---|
| NVLS（NVLink SHARP） | Scale-up，柜内 | 在 NVLink Switch 上做归约 |
| MNNVL | Scale-up，柜内跨 tray | 依赖 IMEX；开启后整个 NVL72 在 NCCL 中算 **1 个 node** |
| CollNet（IB SHARP） | Scale-out，柜间 | 通过 SHARP 插件在 IB 交换机上做归约；Quantum-X800 为 SHARP v4 |
| SAT | Scale-out | IB SHARP 的流式聚合，大消息带宽依赖它（`SHARP_COLL_ENABLE_SAT=1`） |

NCCL 算法选择（NCCL 源码 `src/tuning/nvls.cc`）：单柜（nNodes=1）只能用 `NVLS`；跨柜且 CollNet 关闭时用 `NVLSTree`（柜内 NVLS + 柜间普通 IB Tree）；跨柜且 CollNet 开启时用 `NVLS`（柜内 NVLS + 柜间 IB SHARP）；只测 IB SHARP 用 `CollnetDirect` / `CollnetChain`。NVLS 只支持 SIMPLE 协议，不要同时设 `NCCL_PROTO=LL/LL128`。

**Scale-up：柜内 NVLink SHARP**

柜内 NVLS 与关闭 NVLS 的对比由 L4 的 `superbench.nvl_domain`（`mnnvl` 与 `mnnvl-nvls` 两项）完成，SHARP 测试只做 IB 场景。配置文件中保留的 U-A、U-B 两组不再由 testkit 执行。

**Scale-out：柜间 IB SHARP**（`--scope group`，至少 2 个 rack，例如 R1+R2；开启 MNNVL 后 nNodes = rack 数）

| 组 | 环境变量（均含 `NCCL_MNNVL_ENABLE=1`、`NCCL_CUMEM_ENABLE=1`） | 测量 |
|---|---|---|
| O-A 无 SHARP | `NCCL_NVLS_ENABLE=0`、`NCCL_COLLNET_ENABLE=0` | Ring/Tree 基线 |
| O-B 仅柜内 SHARP | `NCCL_COLLNET_ENABLE=0`、`NCCL_ALGO=NVLSTree` | 柜内 NVLS 对跨柜的收益 |
| O-C 仅柜间 SHARP | `NCCL_NVLS_ENABLE=0`、`NCCL_COLLNET_ENABLE=1`、`NCCL_ALGO=CollnetDirect` | IB SHARP 单独的收益 |
| O-D 两级 SHARP | `NCCL_COLLNET_ENABLE=1`、`NCCL_ALGO=NVLS` | 柜内 NVLS + 柜间 IB SHARP 的完整收益 |

O-C、O-D 另需：SHARP 插件和库加入 `LD_LIBRARY_PATH`（HPC-X 的 `$HPCX_NCCL_RDMA_SHARP_PLUGIN_DIR/lib`、`$HPCX_SHARP_DIR/lib`，以镜像内实际路径为准）、`SHARP_COLL_ENABLE_SAT=1`、调试时 `SHARP_COLL_LOG_LEVEL=3`。

**执行方式**：用 SuperBench 执行（testkit 测试项 `sharp.scale_out`，L4）。`config_gb300_sharp.yaml` 为每个操作、每组设置写一个 nccl-bw 条目（`nccl-bw:sharp:<op>:<组>`），组内的 NCCL 环境变量写在该条目的 mode env 中；所有组都设 `NCCL_DEBUG=INFO`、`NCCL_DEBUG_SUBSYS=INIT,NET,COLL,NVLS,TUNING`，原始输出保存在 SuperBench 的 results.json 中，testkit 从中提取 NVLS、CollNet/SHARP、插件、算法相关的日志行放进报告。对比结果只展示，不判定；某组返回码非 0（例如强制的 `NCCL_ALGO` 不可用）记为失败。

```bash
testkit run sharp.scale_out                         # scope: group，tests.yaml 中 groups: [[R1, R2]]；全部节点时改为 hosts: all
```

**判定**

1. 强制 `NCCL_ALGO` 时，条件不满足 NCCL 会直接报错，不会静默回退；报错即该组失败，并归类为环境问题（IMEX/NMX、UFM SHARP AM、插件）。
2. 日志确认：NVLS 资源分配成功；CollNet/SHARP 插件加载成功；SHARP group 建立成功。控制面解析日志，作为检查项写入 `summary.json`。
3. 带宽：大消息 allreduce busbw 应满足 O-B > O-A、O-D ≥ O-B 且 O-D > O-C；柜内 `mnnvl-nvls` > `mnnvl`；具体倍数在金样本 rack 实测后写入基线。

**前置条件**

- [ ] S7 的 IMEX 已通过（所有参与 rack）；NVLink Switch（NMX）就绪，NVLS 依赖它。
- [ ] Scale-out：UFM 已启用 SHARP AM，交换机支持 SHARP（Quantum-X800 为 SHARP v4）。这一项属于网络侧配置，控制面只做检查。
- [ ] 每组都**显式设置** `NCCL_NVLS_ENABLE=0` 或 `1`，不使用默认值 `2`（原因见下方“NCCL 版本”）；nccl-tests 启用 MPI 编译；镜像内有 SHARP 库和 NCCL SHARP 插件。

**NCCL 版本**（已对照 NCCL 源码和 release notes 核实）

| 版本 | `NCCL_NVLS_ENABLE=2`（默认）时 NVLS 多播内存绑定（`cuMulticastBindMem`）失败 | `NCCL_NVLS_ENABLE=1` 时 |
|---|---|---|
| 2.27.3 – 2.28.x | 只打印 INFO 日志 “Proceeding without NVLS support”，**本 rank** 关闭 NVLS 后继续（2.27.3 release notes：“Gracefully fall back by default to other transports if NVLS initialization fails”） | 报错退出 |
| 2.29.x 及以后 | 报错退出（源码中去掉了对 `=1` 的判断；2.29.2 release notes 写作 “More prominent error when cuMulticastBind fails and NCCL_NVLS_ENABLE=2”） | 报错退出 |

- 2.27/2.28 的回退由每个 rank 自己决定，发生在各 rank 交换拓扑信息之后（`init.cc` 的 allGather3），之后没有再同步。如果只有部分 rank 绑定失败，各 rank 对 NVLS 是否可用的判断会不一致，可能导致后续集合通信挂起。这是根据源码的推断，NCCL 的 release notes 和公开 issue 中没有找到明确的挂起记录。
- 对本项目的影响：测试中显式设置 `NCCL_NVLS_ENABLE`（基线 `0`、NVLS 组 `1`）时，2.27.7 遇到绑定失败也会直接报错，不会静默回退；仓库现有的 GB300 SuperBench 配置都已显式设置。所以 2.27.7 **不是必须升级**，只要不依赖默认值 `2`。
- 仍建议使用 ≥ 2.29 的理由：2.29.2 修复了 “a hang on GB200/300 + CX8 when the user disables GDR”；2.28.9 修复了大规模下主线程与 proxy 线程顺序导致的挂起；主机侧已是 2.31.2，容器内保持一致便于对比。

**测试镜像**：当前 SuperBench worker 镜像（`superbench/main:cuda13.0`，基于 NGC pytorch 25.08）的 NCCL 是 **2.27.7**；镜像内的 HPC-X v2.24.1 带有 SHARP 库和插件。

升级 NCCL 的影响（已核实）：

- NGC PyTorch 镜像中，PyTorch（`libtorch_cuda.so`）和 TransformerEngine 都**动态链接系统的 `libnccl.so.2`**，没有自带 NCCL（在 NGC pytorch 26.10 镜像中用 `ldd` 确认，25.08 打包方式相同）。nccl-tests 由 SuperBench `third_party` 以 `MPI=1` 编译，同样动态链接系统 NCCL。
- 因此直接替换系统 NCCL（`apt install libnccl2=...`）会同时改变 nccl-tests、PyTorch 分布式类测试（SuperBench 的 model benchmarks、`dist-inference`、`sharding-matmul`、`computation-communication-overlap`）、Megatron/DeepSpeed、TransformerEngine 的行为，偏离 NGC 验证过的组合。`libnccl.so.2` 向后兼容，一般能运行，但其他测试的结果会和原镜像不可比。
- 隔离方式（方案 1）只让指定测试加载新 NCCL，其他组件不受影响。
- 其他兼容性：NCCL 2.31.2 支持 v6–v12 版本的 net 插件，HPC-X 自带的 SHARP 插件可以加载；主机 ISO 已使用 `2.31.2-1+cuda13.3` 配 580 驱动和 CUDA 13.0.3（与 DGX OS 一致），容器内使用同一个包。

nccl-tests 与 NCCL 是两件事：nccl-tests 只是测试程序，SHARP/NVLS 的行为由 NCCL 库决定。worker 镜像里的 nccl-tests 是 2025-01 的提交 `cb6a46f`（约 v2.13/v2.14），最新为 v2.21.1。

- 只升 nccl-tests：默认输出列不变（v2.21.1 默认表头与旧版相同），SuperBench `nccl-bw` 的解析器兼容；新增功能用 `#if NCCL_VERSION_CODE` 判断，对 NCCL 2.27.7 编译时自动关闭。影响只限于 `nccl-bw` 测试。但 NCCL 仍是 2.27.7，对 SHARP 测试帮助不大。
- 新版 `-U 1`（`--tuning`）按消息大小打印 NCCL 实际选用的 algo/proto，可直接证明用的是 NVLS 或 CollNet；要求 NCCL ≥ 2.28，并且会在行尾增加列。SuperBench 的解析器从行尾倒数定位 `time`/`busbw` 列，开启后会错位，所以只能在不经 SuperBench 解析的测试中使用。

可选方案：

| 方案 | 做法 | 影响范围 | 主要风险 |
|---|---|---|---|
| 1. 在现有 worker 镜像上加一层（推荐，先做） | `FROM superbench/main:cuda13.0`，把 `libnccl2_2.31.2-1+cuda13.3` 解包到 `/opt/nccl-2.31/lib`，并用它编译 nccl-tests v2.21.1 到 `/opt/nccl-tests-2.21`；系统 NCCL 和 SuperBench 自带的 nccl-tests 都不动 | 只有 S7a 的 SHARP/NVLS 测试使用（MPI 模式，`LD_LIBRARY_PATH` 和二进制路径只在这些测试中指定），可开 `-U 1` 由控制面解析 | 需在日志确认加载的是 2.31.2；镜像多约 1 GB |
| 2. 按上游 `cuda13.4.dockerfile` 重做整个 worker 镜像 | 上游同一提交 `9142bdf` 已提供：NGC pytorch 26.09、CUDA 13.4.1、NCCL 2.31.2、HPC-X 2.51、OpenMPI 5.0.10rc2、torch 2.14；Docker Hub 上暂未确认有现成镜像，需要自己构建 arm64 版本 | **整套软件栈都变**：所有 SuperBench 测试的结果与 cuda13.0 镜像不可比，需重建基线 | 驱动 580 只支持 CUDA 13.0，运行 13.4 依赖小版本兼容，不能用驱动 JIT 编译 PTX（[CUDA 文档](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)），所有测试需在 GB300 上重新验证；OpenMPI 4 → 5，配置中的 `btl: ^openib`、`coll_hcoll_enable` 等 MCA 参数需重新确认；GB300 配置只针对 cuda13.0 镜像做过离线校验；在 x86 上用 QEMU 构建 arm64 镜像需要数小时，建议在 arm64 机器上构建；ISO 预置的 worker 镜像也要更换 |
| 3. 使用主机上的 nccl-tests | 主机 NCCL 2.31.2、HPC-X 已由 ISO 安装；`imex-bench.sh` T3 已用这种方式 | 不需要新镜像 | 不在容器内执行，与“测试统一用容器”的约定不一致 |

M4 先直接用现有 worker 镜像（显式设置 `NCCL_NVLS_ENABLE`）跑通一个 rack；再用方案 1 跑 S7a 并开启 `-U 1` 确认算法。方案 2 作为后续整体升级单独评估：在一个 rack 上与 cuda13.0 镜像对比全部测试结果后再决定是否替换。方案 1 不可行时退回方案 3。

### S8 MoE 测试

使用 `moe-bench` 容器，逐节点启动模式。镜像中有三种 EP 通信库，见 [moe-bench](../benchmarks/moe-bench/README.md)：

| suite | EP 库 | 代表的生产用法 |
|---|---|---|
| `hybrid-ep` | HybridEP（DeepEP `hybrid-ep` 分支，NVIDIA） | Megatron-LM `flex` dispatcher 训练 |
| `deepep` | DeepEP V1（`deep_ep.Buffer`，NVSHMEM） | SGLang `--moe-a2a-backend deepep`；GLM 推理 prefill 用 normal、decode 用 low_latency（见 THUDM/slime `run-glm5.3-744B-A40B.sh`） |
| `moonep` | [MoonshotAI/MoonEP](https://github.com/MoonshotAI/MoonEP)，动态冗余专家 | Kimi 训练 |
| `deepgemm` | 无（单 GPU GEMM） | FP8/FP4/BF16 GEMM |

测试矩阵：

| 档 | scope | 规模 | 通信路径 | hybrid-ep | deepep | moonep | deepgemm |
|---|---|---|---|---|---|---|---|
| 第 1 档 单机 | `node`，全部节点并行 | 1 节点 / 4 GPU | 节点内 NVLink | 4 项 | `intranode`、`low_latency` | `tests`、`bench` | 3 项 |
| 第 2 档 rack 内 | `rack`，每个 rack 一组 | 18 节点 / 72 GPU | MNNVL（依赖 S7 IMEX 通过） | 4 项 | `low_latency` | `tests`、`bench` | rank 0 |
| 第 3 档 跨 rack | `group`，2 个 rack 一组（如 R1+R2、R3+R4） | 36 节点 / 144 GPU | rack 内 NVLink，跨 rack IB | 4 项 | `low_latency` | 不适用 | rank 0 |
| 第 4 档 全部 | `group`，`--targets all` | 72 节点 / 288 GPU | 同上 | 4 项 | `low_latency` | 不适用 | rank 0 |

第 2–4 档由编排按档依次执行 `--suite hybrid-ep`、`--suite deepep`（不适用的项自动 SKIP），第 2 档另加 `--suite moonep`。

- DeepGEMM 是单 GPU 测试，多节点运行时只在 rank 0 执行。
- DeepEP V1 normal 模式固定每个 NVLink 组 8 个 rank，GB300 每节点 4 GPU：`deepep_intranode` 只在 1 节点或 2 节点（MNNVL）运行，`deepep_internode` 报告 SKIP。第 2–4 档的 V1 覆盖以 `low_latency` 为准。
- MoonEP 跨节点用 CUDA fabric handle，没有 RDMA 路径，第 3、4 档不适用。
- SKIP 表示该项不适用于当前拓扑，不算失败。
- 通过：各项无报错，带宽或延迟达到基线（基线在金样本 rack 上建立后写入 `site.yaml`）。第 2 档失败而第 1 档通过的，优先排查 IMEX/NVLink；第 3 档失败而第 2 档通过的，优先排查 IB 计算网；只有 `deepep_low_latency` 跨节点失败的，先检查主机 IBGDA 配置。

### S9 验收报告

- 每台主机：各阶段状态、关键指标、失败原因。
- 每个 rack：IMEX、RDMA 柜内与跨柜、NVLink Fabric（S7b）、MoE 第 2 档结果。
- NVLink Fabric 部分按下表汇总，官方健康状态、正确性、性能三类依据分开列出：

| 顺序 | 验收项目 | 记录的数据 | 通过条件 | 依据类型 |
|---|---|---|---|---|
| 1 | Fabric / IMEX（S5、S7） | 72 GPU Fabric 状态、18 节点 READY、连接矩阵、NVLink 状态、节点内 P2P | 全部正常 | 官方健康状态 |
| 2 | 全 GPU 对可达（N1） | Planned / Passed / Failed，失败 GPU 对 | 2556 / 2556 / 0 | 正确性 |
| 3 | 点对点带宽（N2） | Read、Write 带宽矩阵（GB/s） | 无离群；有基线后不低于基线 | 性能基线 |
| 4 | NVLoom pairwise / all-to-one / multicast（N3） | 带宽统计（GB/s） | `Pass`，无离群 | 正确性 + 性能基线 |
| 5 | NVLoom latency（N3） | 延迟（μs） | `Pass`，无离群 | 正确性 + 性能基线 |
| 6 | NVLoom fabric-stress（N3） | 吞吐、完成状态、错误 | `Pass`，无离群 | 正确性 + 性能基线 |
| 7 | 交换机侧（N5） | 端口状态、错误计数增量、吞吐 | 端口 Active，错误不增加 | 官方健康状态 |
| 8 | NCCL 单机、柜内 72 GPU、全部 rack（`superbench.nccl_node`、`nvl_domain`、`all`、S7a） | `time` / `algbw` / `busbw` / `wrong`，NVLS 是否生效 | `wrong` 为 0，日志确认 MNNVL/NVLS，性能达到基线 | 正确性 + 性能基线 |
| 9 | NVSHMEM（N4，可选） | Put/Get 带宽、延迟 | 只记录 | — |
| 10 | NIXL（`nixl.*`，可选） | 单机 GPU↔GPU、跨节点 RDMA / GPUDirect、存储的带宽与延迟 | 一致性校验通过；有基线后不低于基线 | 正确性 + 性能基线 |
- 跨 rack 与全部节点：MoE 第 3、4 档组级结果。
- 导出 HTML 和 CSV；原始日志打包下载。

## 6. 控制面设计

> **暂不实施。** 本节（状态模型、回调、API、UI、结果存储、部署、`macc`）保留为后续设计参考，当前阶段由 `testkit` 代替，见文首说明。

### 6.1 状态模型

每台主机的每个阶段一条记录：

| 状态 | 含义 |
|---|---|
| `pending` | 未开始 |
| `running` | 执行中 |
| `success` | 通过 |
| `failed` | 失败，附原因和日志 |
| `skipped` | 被 tags 或人工放行跳过，需填写原因 |
| `stale` | 超过时限没有新事件（例如安装中途断电） |
| `outdated` | 之前的步骤被重新执行，本步骤结果失效，需要重测 |

每个步骤的前置条件在服务端统一判断（见 6.4 节流转规则），UI 和 `macc` 都通过同一接口发起执行。

### 6.2 回调事件格式

```json
{
  "source": "run_stage",
  "hostname": "gb300-r1-t01",
  "serial": "1234567890001",
  "stage": "doca_install",
  "status": "failed",
  "rc": 1,
  "ts": "2026-11-07T07:00:00Z",
  "log_tail": "..."
}
```

`source` 取值：`autoinstall`、`cloud-init`、`run_stage`、`poll`（SSH 轮询，主路径）。autoinstall 和 cloud-init 的原生 webhook 格式由服务端适配。

### 6.3 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/events` | 接收主机回调，无认证（仅内网） |
| GET | `/api/v1/hosts` | 主机列表与各阶段状态 |
| GET | `/api/v1/hosts/{hostname}/stages/{stage}/log` | 阶段日志 |
| POST | `/api/v1/inventory` | 上传并校验 `hosts.csv` |
| POST | `/api/v1/runs` | 发起动作：`{action, suite, scope: node/rack/group, targets, nodes, exclude, parallel_groups}`；`macc run` 调用此接口 |
| GET | `/api/v1/runs/{id}` | 执行进度和结果 |
| GET | `/api/v1/steps` | 每一步的主机统计，以及每台主机是否满足前置条件和原因 |
| POST | `/api/v1/hosts/{hostname}/steps/{step}/override` | 人工放行，必须附原因 |
| GET | `/api/v1/runs/{id}/hosts/{hostname}/stream` | 容器实时日志（SSE） |
| GET | `/api/v1/runs/{id}/hosts/{hostname}/results` | `summary.json` 与原始结果下载 |
| POST | `/api/v1/runs/{id}/cancel` | 停止该批次全部容器 |
| GET | `/api/v1/results?stage=&rack=` | 按阶段、rack 汇总所有节点的测试结果 |
| GET | `/api/v1/report` | 导出验收报告 |
| GET/PUT | `/api/v1/baselines` | 查看基线；把某次 run 的指标设为某个阶段/档位的基线（记录来源 run-id、操作人） |

**认证**：除 `/api/v1/events` 外，所有接口需登录（初期用本地账号 + token，`macc` 用同一 token），区分只读和操作两种角色。危险操作（磁盘写测试、网络下发、重启、人工放行）需二次确认，并记录操作人、时间、参数，写入报告。`/api/v1/events` 只接受来源 IP 在 `hosts.csv` 中的请求。

### 6.4 UI

UI 按验收流程做成分步向导：左侧是步骤条，每一步一个页面；上一步通过的主机，才能进入下一步。

**步骤**

| 步骤 | 对应阶段 | 页面主要内容 | 主要操作 | 进入下一步的条件（按主机） |
|---|---|---|---|---|
| 1. 清单 | — | 上传 `hosts.csv`、`site.yaml`；校验错误；与 NICo 的差异 | 上传、校验、`nico-sync` | 清单校验通过 |
| 2. 装机监控 | S0–S2 | rack × tray 矩阵，每台主机的安装阶段与失败日志 | 查看日志；失败主机提示去 NICo 重装 | 首启全部阶段 `success` |
| 3. 网卡识别 | S3 | 每台主机的网卡映射、BF3 模式、与金样本的差异 | 执行识别；把一台主机标为金样本并确认 | 映射与金样本一致，BF3 为 NIC 模式 |
| 4. 网络配置 | S4 | 每台主机将要下发的 IP、MTU、IB 模式（执行前预览差异） | 预览、执行、需要时重启 | IP、MTU、IB 状态、连通性全部通过 |
| 5. DCGM | S5 | 每台主机的诊断结果、实时日志 | 执行、取消、重跑失败主机 | diag 全部 Pass |
| 6. SuperBench | S6 | SB-1 单机（含磁盘）、SB-2–SB-4 IB 多节点结果，按 `rule_gb300.yaml` 分类比对；每台主机的测试盘解析结果 | 按档执行、取消、重跑；首次磁盘测试确认目标盘 | SB-1–SB-4 按 `rule_gb300.yaml` 通过 |
| 7. IMEX 与 SHARP | S7、S7a | 每个 rack 的 IMEX 状态矩阵、T1–T3 结果、SuperBench nvl-domain 结果；柜内 U-A/U-B、柜间 O-A~O-D 的 busbw 对比曲线和日志检查项 | 配置、检查、测试；按 rack 跑 scale-up，按 rack 组合跑 scale-out | rack 内全部 READY，T1–T3 与 NCCL-MNNVL 通过，NVLS 对比通过；scale-out 对比通过（单 rack 交付时不适用） |
| 8. MoE | S8 | 第 1–4 档的组级和节点级结果 | 按档执行（对应 `macc run` 的 scope） | 第 1–4 档达到基线 |
| 9. 报告 | S9 | 全部结果汇总 | 导出 HTML、CSV、原始日志 | — |

**流转规则**

- 放行按主机计算，不必等全部主机都完成：例如 R1 网络配置全部通过后，就可以先对 R1 做 DCGM，R2 仍停在网络配置这一步。
- 每一步页面上只能选中满足前置条件的主机。没满足的主机置灰，鼠标悬停显示卡在哪一步、原因是什么。
- 多节点步骤（IMEX、MoE 第 2–4 档）要求组内所有节点都满足前置条件；组内有未满足的节点时，提示用 `--exclude` 排除，或者等它就绪。
- 某台主机重新执行了前面的步骤（例如重配网络），它后续步骤的结果自动标记为 `outdated`，需要重新测试。
- 人工放行：在确有需要时（例如某项已知问题），可以对单台主机跳过某一步，必须填写原因；报告中会列出所有人工放行记录。
- 每一步都可以对失败主机单独重跑，不影响已通过的主机。

**公共部分**

- 顶部：每一步的主机数统计（通过/失败/执行中/未开始），点击可跳到对应步骤并筛选。
- 主机详情：所有步骤的时间线、日志、网卡映射、版本信息、测试指标。
- 执行面板：显示当前批次进度、容器实时日志，可取消。
- 测试结果：可按 rack、指标排序和筛选，与基线比较，不达标标红；多节点测试另有组级视图（节点、rank 分配、组级指标、最先失败的节点）。
- 基线管理：选定一次结果正常的 run（通常是金样本 rack），一键设为对应阶段/档位的基线；可设置允许偏差（如 −5%）。DCGM 和 SuperBench 以自带判定/`rule_gb300.yaml` 为准，IMEX、MoE 依赖此处的基线。
- 命令行 `macc` 与 UI 使用同一套前置条件检查，绕过 UI 也不能跳步。

### 6.5 结果存储

- SQLite：主机、阶段状态、事件、执行记录、节点执行锁、基线、`summary.json` 中的指标和检查项、操作审计。
- 文件：`runs/<run-id>/<hostname>/<stage>/`，保存从节点拉回的完整结果目录。
- 均放在 PVC 上。节点上的结果目录在拉回成功后按保留天数清理。

### 6.6 部署

- 镜像：`daocloud/metal-deployer/acceptance-controller`（Python + FastAPI + Ansible + Docker CLI + 前端静态文件）。
- 前端：Vue 3 + Vite，构建为静态文件由 FastAPI 一并提供，部署只有一个 Pod；实时日志用 SSE。
- Kubernetes：在 NICo 集群中独立 namespace；Deployment（1 副本）+ PVC + Service（NodePort 或 LoadBalancer，供 UI 访问）。Pod 出向 SSH 经节点 SNAT 后源地址为 NICo 节点 IP，主机回包由 LOM 策略路由送回，不需要额外声明网段；启用主机回调时，Service 地址还需 LOM 网可达。
- Secret：SSH 私钥、NICo 只读 token、可选 BMC 凭据。ConfigMap：`site.yaml`、`hosts.csv`。SSH 公钥经 `build-iso/config/ssh_authorized_keys` 打进 ISO，首启由 `configure_ssh.sh` 写入 root，并设置 `PermitRootLogin prohibit-password`（root 只能用密钥登录）。
- 控制面 Pod 本身不需要 GPU，也不运行测试容器；测试容器都在 GPU 节点上运行。
- 不修改 NICo 组件，不使用 NICo 的写接口。

### 6.7 回调地址如何传给主机（可选）

主机回调只用于实时刷新进度，装机进度的主路径是 SSH 轮询（S2）。启用回调时，还需确认 LOM 网段能访问控制面的回调端口（ACL 只确认了控制面到 LOM 的 SSH，反方向需单独确认）。

依据 NICo 文档 [iPXE Script Variables](https://docs.nvidia.com/infra-controller/documentation/provisioning-day-0/i-pxe-script-variables)、[Templated iPXE Operating Systems](https://docs.nvidia.com/infra-controller/documentation/configuration-day-1/templated-i-pxe-operating-systems)、[Tenant Management – Phone-home](https://docs.nvidia.com/infra-controller/documentation/configuration-day-1/tenant-management#phone-home)：

- NICo 装系统靠“Operating System 定义 + Instance”。OS 定义有三种：自定义 `ipxeScript`、iPXE 模板（如 `kernel-initrd`，带 `kernel_params` 参数）、`imageUrl`（直接写盘镜像）。
- 主机 PXE 启动时，NICo 渲染 iPXE 脚本，可引用 `${tenant-cloudinit-url}`。该地址是一个 NoCloud 数据源，内容是 OS 定义里的 `userData`；OS 定义开启 `allowOverride` 时，Instance 可以覆盖它。
- 典型内核参数：`ip=dhcp autoinstall ds=nocloud-net;s=${tenant-cloudinit-url}`。

因此 NICo 有两个注入点：

| 方式 | 做法 | 对本 ISO 的影响 |
|---|---|---|
| A. 内核参数（推荐） | 在 OS 定义的 iPXE 脚本或 `kernel_params` 里追加 `md.report_url=http://<地址>:<端口>` | ISO 内的 autoinstall 配置不变。`early-commands` 从 `/proc/cmdline` 读取，`late-commands` 写入目标系统 `/etc/metal-deployer/report.env`，`run_stage` 读取后上报。参数缺失时不上报，不影响装机 |
| B. NICo `userData` | 内核参数用 `ds=nocloud-net;s=${tenant-cloudinit-url}`，把整份 autoinstall 配置放进 NICo 的 `userData` | ISO 内嵌的 user-data 不再生效，配置改由 NICo 管理；回调地址直接写进 `userData` |

采用 A：改动小，地址变化时只改 NICo 的 OS 定义，不重建 ISO。

NICo 还有自带的 phone-home：OS 定义设 `phoneHomeEnabled: true` 后，NICo 往 `userData` 注入 cloud-init `phone_home`，回调到站点元数据服务（默认 `http://169.254.169.254/latest/meta-data/phone_home`），收到前不把 Instance 标为 Ready。它只表示“系统已启动”，不含阶段细节；并且元数据服务在 DPU 模式下由 DPU 提供，NIC 模式下是否可达需要现场确认。可作为补充，不替代本项目的回调。

## 7. 里程碑

| 里程碑 | 内容 | 交付物 | 依赖 | 完成标准 | 状态 |
|---|---|---|---|---|---|
| M0 | 计划与格式确定 | 本文；`cluster.yaml`、`tests.yaml` 模板（`testkit init`）；IP 规划表导入（`testkit import`） | — | 评审通过 | 完成 |
| M1 | 装机进度 | ISO 内 `state.json`（S2）；控制面服务、UI、K8s 部署、`nico-sync`、回调 | M0；控制面 SSH 公钥打进 ISO | 当前阶段：可经 LOM SSH 查看 `state.json` 和阶段日志 | `state.json` 已实现；其余暂不实施 |
| M2 | 网卡识别与网络配置 | S3、S4（`testkit nic`、`testkit net`，含 `--dry-run` 差异预览、自动回滚） | M0；第一台实机 | 金样本确认；一个 rack 批量配置通过，重复执行无变更 | 已实现，实机待确认 |
| M3 | 容器测试框架 + DCGM 诊断 | testkit 执行器（远程 Docker、多节点批量启动三种模式、实时日志、结果拉回、取消）；S5 | M2 | 一个 rack 并发执行 DCGM 并拿到全部节点结果；用最小多节点示例验证一条命令批量启动和失败联动停止 | 已实现，实机待确认 |
| M4 | SuperBench + IMEX + NVLink Fabric + SHARP | S6、S7、S7a 编排与结果解析；S7b（`nvlink.pairs`、`nvlink.fabric`，主机模式；`nvshmem.*` 可选）；分层关卡与链路计数器；SHARP 测试镜像 | M3；UFM（含 SHARP AM）、NMX 就绪；NMX-T 地址 | 一个 rack 全部通过或给出明确的失败归类；在金样本 rack 上确定 S7b 各步耗时和性能基线；有 2 个 rack 时完成 O-A~O-D 对比 | S6、S7、S7a、S7b（N1–N4）、分层关卡已实现（单元测试与 e2e 通过），实机待确认；N5 NMX-T 待做 |
| M5 | MoE 测试 | S8 第 1–4 档编排；HybridEP、DeepEP V1、MoonEP 实机验证 | M4 | 四档都能一条命令启动并出结果；建立基线 | 待做 |
| M6 | 验收报告 | S9：`testkit report` 生成 HTML、CSV，可打包原始日志 | M5 | 全部主机一份报告 | 报告框架已实现，随各测试项补充 |

各阶段的实现进度以 [testkit README](../benchmarks/testkit/README.md) 的进度表为准。

## 8. 风险与待确认

| 项 | 影响 | 处理 |
|---|---|---|
| 回调地址尚未确定；LOM 网段到控制面回调端口是否放通未确认 | S1、S2 实时性 | 回调为可选；进度主路径为 LOM SSH 轮询，不依赖回调 |
| 控制面经 LOM SSH | 全部 SSH | 网络侧已确认 OOB ACL 放开 NICo 节点到 LOM 网段的 SSH，无限制；testkit 控制机不在 NICo 节点上时，需确认它到 LOM 网段的 SSH 同样放通 |
| S4 切换默认路由到 BF3 port 0 后，LOM 上 SSH 回包改走 port 0，控制面失联 | S4 及之后全部 SSH | LOM 加策略路由（复用 LOM 的 netplan id，已在 QEMU 验证）；应用前设 120 秒定时回滚；BF3 配错时可用 BMC 串口控制台排障 |
| GB300 实机 LOM 网卡型号、BF3 PCI ID（`15b3:a2dc`）未在实机确认 | S3、S4 | 金样本主机上 `lspci -nn`、`ip route get <NICo 地址>` 确认 |
| S4 网络配置下发方案未最终确定（SSH 推送 / NICo userData 拉取 / NICo DHCP） | S4、ISO | 见 3.3 节；默认按方案 A 实现，选 B 需在 ISO 加通用拉取步骤，选 C 需网络侧配 DHCP relay |
| S7b 依赖的 DCGM multinode 包未在 GB300 实机验证；nv-hostengine 默认监听地址未确认 | S7b | 在金样本 rack 上先跑 2 节点 `mnnvbandwidth`；监听地址按 S7b“执行方式”第 2 步处理 |
| NVIDIA 没有公开 GB300 NVL72 统一的 NVLink 带宽和延迟门限 | S7b、S9 判定 | 先做 rack 内离群检测；向 NVIDIA/OEM 索取平台基线，并用金样本 rack 实测值确定门限 |
| NMX-T 地址、认证和 fieldset 未知 | S7b N5 | 由网络侧提供；不可用时 N5 记为 SKIP，报告注明缺少交换机侧数据 |
| UFM IPoIB partition MTU 实际值 | S4 的 MTU 校验 | MTU 在 `site.yaml` 声明，按现场实际调整 |
| 控制面能否经 OOB 访问 DPU BMC | S3 的 Redfish 校验 | 不能则只用 `mlxconfig` |
| GB300 实机 PCI 拓扑与假设不同 | S3、S6 | 金样本确认后更新本文第 2 节 |
| CX8 改模式需要重启，可能触发 NICo 侧状态变化 | S4 | 先在一台验证 NICo 的反应 |
| MoonEP 是否支持 SM103（GB300）和 MNNVL 跨节点未经验证；DeepEP V1 low_latency 依赖的 IBGDA 主机配置未确认 | S8 | M5 先在金样本单机跑 `--suite moonep`、`--suite deepep`，再在一个 rack 上跑第 2 档 |
| SuperBench worker 镜像 NCCL 为 2.27.7：默认 `NCCL_NVLS_ENABLE=2` 时 NVLS 绑定失败会按 rank 各自静默回退，部分 rank 失败时可能不一致甚至挂起（源码推断，未见官方记录） | S7、S7a | 所有测试显式设置 `NCCL_NVLS_ENABLE=0/1`；设 `run_timeout`，超时即判失败；可选升级到 NCCL 2.31.x |
| IB SHARP 依赖 UFM 启用 SHARP AM 和交换机支持 | S7a scale-out | 网络侧确认；O-C/O-D 报错时归类为网络侧问题 |
| 飞书文档中 `SHARP_COLL_*` 变量和插件路径来自 HPC-X 惯例，不同版本可能不同 | S7a | 以镜像内 `$HPCX_*` 变量和实际文件为准，M4 实机确认 |
| DCGM 容器模式：连主机 hostengine（容器内 `dcgmi` 版本须与主机 4.7.1 一致，诊断实际在主机侧执行），或容器内独立运行 hostengine（测试期间需停主机 `nvidia-dcgm` 服务） | S5 | M3 在一台实机上对比后选定 |
| SuperBench worker 镜像内的 MPI、nccl-tests、nvbandwidth 能否直接用于 IMEX T1–T3；容器内 IMEX channel 设备是否可见 | S7 | M4 验证；不行则为 IMEX 测试单独做镜像 |
| sbcli 控制镜像的架构须与控制面节点一致（x86 用 `make build-sbcli`，arm64 用 `make build-sbcli-arm64`）；profile 不能依赖自动判断 | S6、S7 | 固定传 `-p gb300` |
| SuperBench GB300 配置只做过离线校验，未上实机 | S6、S7 | 金样本主机先完成 [superbench-gb300.md](./superbench-gb300.md) 第 6 节确认项 |
| GB300 autoinstall 用 `match: size: largest` 选系统盘；GB300 的 E1.S（3.84/4 TB）大于 M.2（1.92/2 TB），系统会装到 E1.S 上，且各主机装到哪块不确定 | S1、S6 磁盘测试、后续 `/raid` | 已改：[manifest-gb300.yaml](../build-iso/manifests/manifest-gb300.yaml) 改为 `size: smallest`，安装时由 `select_root_disk.sh` 检查，可用内核参数 `md.root_disk` 覆盖。待办：用 NICo 硬件清单确认 M.2 在全部机器上都是最小盘；S1 校验根文件系统在 M.2 上；在 GB300 实机上验证一次安装 |
| 有无内网镜像仓库；镜像体积大（moe-bench 约 10.9 GB），72 台推送耗时 | S5–S8 | 优先内网仓库或 ISO 预置；否则控制面按 rack 分批推送 |
