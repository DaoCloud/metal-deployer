# moe-bench：GB300 MoE 通信与计算测试镜像

本镜像用于 GB300 / B300（Blackwell Ultra，sm_103，arm64/SBSA）的 MoE 测试。MoE（Mixture of Experts，混合专家模型）需要专家并行（EP）通信和分组 GEMM 计算，本镜像同时覆盖这两部分。

镜像中有三种 EP 通信库，对应不同的使用方：

| EP 库 | Python 接口 | 使用方 | 节点内 / NVL72 机柜内 | 跨机柜 |
|---|---|---|---|---|
| HybridEP | `deep_ep.HybridEPBuffer` | Megatron-LM `flex` dispatcher（NVIDIA） | CUDA fabric handle，支持 MNNVL | DOCA GPUNetIO（GPU 发起 RDMA） |
| DeepEP V1 | `deep_ep.Buffer` | SGLang `--moe-a2a-backend deepep`，GLM 推理（prefill 用 normal，decode 用 low_latency） | normal：CUDA IPC 或 fabric handle，最多 8 个 rank | NVSHMEM（IBGDA） |
| MoonEP | `moonep.Buffer` | Kimi（Moonshot）训练，动态冗余专家 | CUDA VMM，跨节点用 fabric handle | 不支持 |

HybridEP 和 DeepEP V1 来自 DeepEP `hybrid-ep` 分支的同一个 `deep_ep` 包，分别编译成 `hybrid_ep_cpp` 和 `deep_ep_cpp` 两个扩展。NVSHMEM 只链接到 `deep_ep_cpp`，HybridEP 不使用 NVSHMEM。

## 镜像内容

| 组件 | 版本 | 说明 |
|---|---|---|
| 基础镜像 | `nvcr.io/nvidia/pytorch:26.03-py3`（arm64 digest 固定） | Ubuntu 24.04、Python 3.12 |
| CUDA | 13.2.0 | 来自基础镜像，运行时 JIT 编译需要完整工具链 |
| PyTorch | 2.11.0a0+a6c236b9fd1 | 来自基础镜像 |
| NCCL（运行时） | 2.29.7 | 来自基础镜像，`torch.distributed` 使用 |
| DeepEP | `hybrid-ep` 分支 `4e890fd` | HybridEP（NVIDIA 贡献）和 DeepEP V1（normal、low_latency） |
| NVSHMEM | 3.5.21：基础镜像自带 host 库，`nvidia-nvshmem-cu13` wheel 提供 `libnvshmem_device.a`；`libnvshmem3-dev-cuda-13` 提供 perftest（`/usr/bin/nvshmem_13/perftest`） | DeepEP V1 跨节点和 low_latency 路径使用；DeepEP 要求 3.3.9 及以上。wheel 和 dev 包版本必须与基础镜像一致，构建时检查。perftest 供 mtest 的 `nvshmem.*` 测试项使用（put/get 带宽和延迟、alltoall 延迟） |
| NCCL（HybridEP 构建用） | 2.29.2，DeepEP submodule `1e0c869` | 只用于生成 DOCA GPUNetIO 对象，不替换运行时 NCCL |
| MoonEP | `master` 分支 `d86fda4` | 源码保留在 `/opt/MoonEP`，测试和 benchmark 从这里运行 |
| CUTLASS DSL | `nvidia-cutlass-dsl[cu13]` 4.6.2 | MoonEP 的 CuTe DSL kernel，运行时编译。基础镜像自带 4.3.5，安装时被升级；`protobuf` 也因依赖约束从 7.x 降到 6.x |
| DeepGEMM | `main` 分支 `5081f85` | FP8/FP4/BF16 GEMM 和 Mega MoE |
| rdma-core | v60.0（`89ddad4`） | HybridEP RDMA 路径要求的版本 |
| GPU 架构 | `TORCH_CUDA_ARCH_LIST=10.3` | GB300、B300 |

HybridEP 使用 DOCA/RDMA 后端（`HYBRID_EP_MULTINODE=1`，`USE_NIXL=0`）。上游的 NIXL 后端仍是实验功能，本镜像不包含。

构建时会检查 `deep_ep_cpp` 是否链接了 `libnvshmem_host`。没有链接时构建失败，避免 DeepEP V1 的跨节点和 low_latency 功能被静默关闭。

`hybrid-ep` 分支不能直接和 NVSHMEM 3.5 一起编译：链接时 `nvshmemi_ibgda_device_state_d` 重复定义，且 3.5 的 QP 布局变了。[patches/deepep-nvshmem-3.5.patch](image/patches/deepep-nvshmem-3.5.patch) 取自上游 deepseek-ai/DeepEP#564（已合入 main）和 deepseek-ai/DeepEP#623（针对 hybrid-ep，未合并），只保留修复部分。另外 CUDA 13 把 CCCL 头文件移到 `include/cccl`，构建时通过 `CPATH` 加入。

基础镜像中 PyTorch 已链接 NVSHMEM 3.5.21，运行时进程只会加载这一份 `libnvshmem_host.so.3`。因此 `NVSHMEM_VERSION` 必须与基础镜像一致，升级基础镜像时要同时修改。

主线 DeepEP V2.5 不在本镜像中。它与 `hybrid-ep` 分支的 Python 包名都是 `deep_ep`，不能装在同一个环境中。MoonEP 的 `benchmarks/bench_vs_deepep.py` 以 V2 为对比对象，因此本镜像不运行它。

## 构建

在仓库根目录执行：

```bash
make build-moe-bench                       # 默认 linux/arm64，标签 latest
make build-moe-bench MOE_BENCH_IMAGE_TAG=dev MOE_BENCH_MAX_JOBS=32
make test-moe-bench MOE_BENCH_IMAGE_TAG=dev  # 不需要 GPU，打印构建信息和测试列表
```

如果主机不是 arm64，必须先启用 QEMU binfmt。在 56 核 x86 主机上用 QEMU 模拟构建，耗时约 3 小时，其中大部分时间用于编译 NCCL submodule 和 CUDA 扩展。在 arm64 主机或 GB300 节点上构建要快得多。加入 NVSHMEM 和 MoonEP 之前，构建出的镜像约 10.9 GB。

拉取 `nvcr.io` 镜像不需要登录。如果网络无法访问 GitHub，用 `--build-arg GITHUB_URL=<镜像地址>` 替换。

## 运行

每个节点启动一个容器。容器需要 GPU、RDMA 设备、主机网络和较大的共享内存：

```bash
docker run -d --name moe-bench \
  --gpus all --privileged --network host --ipc host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v /tmp/moe-bench:/tmp/moe-bench \
  daocloud/metal-deployer/moe-bench:latest
```

`--privileged` 用于访问 `/dev/infiniband`、`/dev/gdrdrv` 和 IMEX channel 设备。如果不使用特权模式，必须逐个映射这些设备。

先记录环境：

```bash
docker exec moe-bench moe-bench-env
```

报告包含 GPU 型号、驱动、compute capability、NVLink fabric（MNNVL/IMEX）状态、软件版本和 RDMA 设备。如果 compute capability 不是 10.3，报告会给出警告。

### 单节点

```bash
docker exec moe-bench moe-bench                  # hybrid-ep + deepep + moonep + deepgemm
docker exec moe-bench moe-bench --suite hybrid-ep
docker exec moe-bench moe-bench --suite deepep   # GLM 推理使用的 DeepEP V1
docker exec moe-bench moe-bench --suite moonep
docker exec moe-bench moe-bench --tests mega_moe
docker exec moe-bench moe-bench --list
```

默认每节点进程数等于可见 GPU 数。GB300 计算节点为 4。

### 多节点

每个节点执行同一条命令。`WORLD_SIZE` 是节点数，`RANK` 是节点编号，不是 GPU 编号：

```bash
# 节点 0
docker exec -e WORLD_SIZE=2 -e RANK=0 -e MASTER_ADDR=<节点0 IP> -e MASTER_PORT=29500 \
  moe-bench moe-bench --suite hybrid-ep
# 节点 1
docker exec -e WORLD_SIZE=2 -e RANK=1 -e MASTER_ADDR=<节点0 IP> -e MASTER_PORT=29500 \
  moe-bench moe-bench --suite hybrid-ep
```

测试脚本自己为每个 GPU 启动进程：HybridEP、DeepEP V1、DeepGEMM 测试用 `torch.multiprocessing`，MoonEP 测试由 `moe-bench` 调用 `torchrun`。不要在外面再用 `torchrun` 按 GPU 启动，否则进程数会重复。

同一 NVL72 机柜内，HybridEP 会检测 NVLink 域。如果 NVLink 域跨多个节点（MNNVL），测试日志会打印 `[topology] ranks_per_nvlink_domain=...`。可以用 `NUM_OF_HYBRID_EP_RANKS_PER_NVLINK_DOMAIN` 覆盖检测值。跨机柜时，流量走 RDMA。

DeepGEMM 的 `layout`、`bf16`、`fp8_fp4` 是单 GPU 测试，多节点运行时只在 `RANK=0` 的节点执行。

## 测试项

| 名称 | 来源 | 内容 |
|---|---|---|
| `hybrid_ep` | DeepEP `tests/test_hybrid_ep.py` | BF16/FP8 dispatch、combine、permute 正确性和带宽 |
| `graphed_hybrid_ep` | `tests/test_graphed_hybrid_ep.py` | CUDA Graph 捕获与重放 |
| `ragged_hybrid_ep` | `tests/test_ragged_hybrid_ep.py` | 各 rank token 数不同、零 token rank |
| `hybrid_ep_token_drop` | `tests/test_hybrid_ep_token_drop.py` | 专家容量溢出时的 token 丢弃 |
| `deepep_intranode` | DeepEP `tests/test_intranode.py` | V1 normal 模式（GLM prefill），总 rank 数不超过 8；多节点时加 `--allow-mnnvl --use-fabric` |
| `deepep_internode` | `tests/test_internode.py` | V1 normal 模式跨节点。上游要求每节点 8 GPU，GB300（每节点 4 GPU）上报告 SKIP |
| `deepep_low_latency` | `tests/test_low_latency.py` | V1 low_latency 模式（GLM decode），NVSHMEM IBGDA；多节点时加 `--allow-mnnvl` |
| `moonep_tests` | MoonEP `tests/test_*.py`（pytest） | planning、dispatch、combine、e2e、grad_reduce、prefetch、API 校验 |
| `moonep_bench` | `benchmarks/bench_comm.py` | 按 hidden、topk、EP 规模、不均衡度扫描各算子耗时，结果写 CSV |
| `layout` | DeepGEMM `tests/test_layout.py` | 缩放因子布局转换 |
| `bf16` | `tests/test_bf16.py` | BF16 GEMM 正确性和性能 |
| `fp8_fp4` | `tests/test_fp8_fp4.py` | FP8/FP4 GEMM 和分组 GEMM |
| `mega_moe` | `tests/test_mega_moe.py` | 融合 EP 通信与 GEMM，默认不执行 |

HybridEP 测试规模用环境变量调整，例如 `HIDDEN_DIM`、`NUM_TOKENS_PER_RANK`、`NUM_LOCAL_EXPERTS`、`TOPK`、`NUM_SMS_DISPATCH`、`NUM_SMS_COMBINE`。

DeepEP V1 和 MoonEP 用下面的环境变量调整：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPEP_ALLOW_MNNVL` | `1` | 多节点时允许 DeepEP V1 跨节点走 NVLink。设为 `0` 时 low_latency 只走 RDMA |
| `DEEPEP_LL_NUM_EXPERTS` | 不小于 288 的、rank 总数的最小倍数 | `deepep_low_latency` 的专家数。上游要求专家数能被 rank 总数整除 |
| `MOONEP_MEM_HANDLE_TYPE` | `auto` | 上游变量。`auto` 在跨节点时用 fabric handle，`fd` 只能在节点内使用 |
| `MOONEP_BENCH_ARGS` | 空 | 传给 `bench_comm.py` 的额外参数，例如 `--ep 64 --hidden 7168` |

不适用于当前拓扑的测试报告为 SKIP，不算失败。在 GB300 上，`deepep_intranode` 只在 1 节点或 2 节点（8 个 rank）时运行，`deepep_internode` 总是 SKIP。

## 日志

日志默认写入 `/tmp/moe-bench/<时间>/`，每个测试一个文件：

- `<主机>-rank<N>-env.log`：环境报告
- `<主机>-rank<N>-<测试>.log`：测试输出
- `<主机>-rank<N>-summary.txt`：PASS/FAIL/SKIP 和耗时
- `<主机>-rank0-moonep_bench.csv`：MoonEP 各算子耗时，只在节点 rank 0 写入

有任何测试失败时，`moe-bench` 返回非零退出码。PASS 只表示正确性检查通过。带宽和延迟数据在测试日志中，验收门限要按实际平台另外确定。

## 已知限制

- 本镜像在 QEMU 中完成了构建和无 GPU 的导入检查，没有在 GB300 硬件上运行。
- NVSHMEM 补丁中 3.5 QP 布局的改动只在上游 H100 上测试过，GB300 上的 low_latency 需要实机确认。
- DeepEP V1 normal 模式固定每个 NVLink 组 8 个 rank（`NUM_MAX_NVL_PEERS=8`）。GB300 每节点 4 GPU，跨节点 normal 模式只能在 2 节点内借助 MNNVL 运行（`deepep_intranode`），更大规模的 V1 测试只有 low_latency。
- DeepEP V1 low_latency 需要 NVSHMEM IBGDA。主机需要配置 `NVreg_EnableStreamMemOPs=1`、`PeerMappingOverride=1`，或者使用依赖 GDRCopy 的 CPU-assisted IBGDA。需要在实机上确认是哪一种。
- MoonEP 跨节点依赖 fabric handle，只能在同一个 NVL72 机柜内运行（L1、L2），跨机柜（L3、L4）不适用。MoonEP 是否支持 SM103 需要在实机上确认。
- `moonep_bench` 只扫描不大于 rank 总数的 EP 规模（4、8、16、32、64），每个 EP 组固定由前 N 个 rank 组成。
- 主机 GPU 驱动为 R580 时，CUDA 13.2 依赖小版本兼容。HybridEP 和 DeepGEMM 在运行时编译 kernel，必须在实机上确认 JIT 可以正常运行。
- HybridEP 链接 rdma-core v60.0，PyTorch 的 NCCL 使用基础镜像中的 rdma-core 56.0。上游示例采用同样方式，但两者共存需要在实机上确认。
- 上游 HybridEP 测试数据只有 H100、B200、GB200，没有 GB300 基线。
