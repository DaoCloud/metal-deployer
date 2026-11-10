# testkit：GB300 批量测试工具（mtest）

> 状态：开发中。已可用 `init`、`validate`、`plan`、`doctor`、`run`、`status`、`logs`、`cancel`、`report`；目前可运行的测试项只有 `smoke`，其余测试项按 [实施计划](#实施进度) 逐步实现。

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
benchmarks/testkit/e2e/run-e2e.sh [--keep]   # 端到端测试，约 3 分钟
```

端到端测试不需要 GPU 节点：在本机用 Docker 启动 3 个“假节点”（sshd + docker CLI，使用本机 Docker），再经真实 SSH 执行 `mtest`，覆盖 per-node、mpi、delegate 三种方式、单台失败、超时、`mtest cancel`、SIGTERM、`status`、`logs`、`report`。需要本机有 Docker，首次运行会拉取 `docker:cli`、`debian:bookworm-slim` 并安装 OpenMPI。网段默认 172.28.0.0/24、172.29.0.0/24，冲突时用 `SSH_NET`、`MGMT_NET` 环境变量修改。

只用标准库和 PyYAML，语法保持兼容 Python 3.8。

## 实施进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 包结构、命令框架、测试 | 完成 |
| P1 | 配置加载与校验、主机选择、分组与 rank、`init/validate/plan/doctor` | 完成 |
| P2 | 执行器：SSH、远程 Docker、三种启动模式、结果拉回、`run/status/logs/cancel` | 完成 |
| P6 | HTML 报告 | 完成 |
| P3 | S3 网卡识别、S4 网络配置 | 待做 |
| P4 | S5 DCGM | 待做 |
| P5 | S6 SuperBench（含 RDMA 打流） | 待做 |
| P7 | S7 IMEX、S7a SHARP | 待做 |
| P8 | S8 MoE | 待做 |
| P9 | testkit 镜像、文档 | 待做 |
| P10 | SuperBench 磁盘测试（安全检查） | 待做 |
