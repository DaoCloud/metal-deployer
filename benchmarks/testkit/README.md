# testkit：GB300 批量测试工具（mtest）

> 状态：开发中。已可用 `init`、`validate`、`plan`、`doctor`；其余子命令按 [实施计划](#实施进度) 逐步实现。

`mtest` 在一台控制机上经 SSH 管理一批 GB300 节点：识别网卡、下发网络配置，然后在各节点用 Docker 容器运行 DCGM、SuperBench、IMEX、SHARP、MoE 等测试，把结果拉回本机，生成一个 HTML 报告。设计见 [gb300-acceptance-plan.md](../../docs/gb300-acceptance-plan.md)。

## 运行方式

两种方式使用同一份代码：

| 方式 | 依赖 | 说明 |
|---|---|---|
| 直接运行 | Python ≥ 3.8、PyYAML、`ssh`；可选 `rsync`、`sb` | `pip install ./benchmarks/testkit` 后使用 `mtest`；开发时也可 `cd benchmarks/testkit && python3 -m mtest` |
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
```

只用标准库和 PyYAML，语法保持兼容 Python 3.8。

## 实施进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 包结构、命令框架、测试 | 完成 |
| P1 | 配置加载与校验、主机选择、分组与 rank、`init/validate/plan/doctor` | 完成 |
| P2 | 执行器：SSH、远程 Docker、三种启动模式、结果拉回、`run/status/logs/cancel` | 待做 |
| P6 | HTML 报告 | 待做 |
| P3 | S3 网卡识别、S4 网络配置 | 待做 |
| P4 | S5 DCGM | 待做 |
| P5 | S6 SuperBench（含 RDMA 打流） | 待做 |
| P7 | S7 IMEX、S7a SHARP | 待做 |
| P8 | S8 MoE | 待做 |
| P9 | testkit 镜像、文档 | 待做 |
| P10 | SuperBench 磁盘测试（安全检查） | 待做 |
