# testkit：GB300 批量测试工具（mtest）

> 状态：开发中。当前只有命令框架（P0），各子命令按 [实施计划](#实施进度) 逐步实现。

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

`mtest init` 生成带注释的模板（P1）。

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
| P1 | 配置加载与校验、主机选择、分组与 rank、`init/validate/plan/doctor` | 待做 |
| P2 | 执行器：SSH、远程 Docker、三种启动模式、结果拉回、`run/status/logs/cancel` | 待做 |
| P6 | HTML 报告 | 待做 |
| P3 | S3 网卡识别、S4 网络配置 | 待做 |
| P4 | S5 DCGM | 待做 |
| P5 | S6 SuperBench（含 RDMA 打流） | 待做 |
| P7 | S7 IMEX、S7a SHARP | 待做 |
| P8 | S8 MoE | 待做 |
| P9 | testkit 镜像、文档 | 待做 |
| P10 | SuperBench 磁盘测试（安全检查） | 待做 |
