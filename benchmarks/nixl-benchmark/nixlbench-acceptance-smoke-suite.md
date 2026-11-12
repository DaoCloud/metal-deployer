# NIXLBench 验收 smoke 套件（S1-S6）

`testNixl`（镜像内置）和 mtest 的 `nixl.node` / `nixl.pair` 使用同一组测试项。目标是在交付验收时用较短时间确认：节点内 GPU 互传、跨节点 RDMA（含 GPUDirect）带宽与延迟正常，数据正确。

## 测试项

| 项 | 范围 | initiator → target | 操作 | 块大小 | 总缓冲 | 其他 | 关注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | 单机，2 张 GPU | VRAM → VRAM | WRITE | 4K–64M | 256 MiB | NVLink / cuda_ipc | 大块带宽 |
| S2 | 单机，2 张 GPU | VRAM ← VRAM | READ | 4K–16M | 64 MiB | 一致性校验 | 带宽 + 数据正确 |
| S3L | 单机（可选，仅 testNixl） | DRAM → DRAM | WRITE | 4K–16M | 256 MiB | 网卡环回 | 连通性 |
| S3 | 两台主机 | DRAM → DRAM | WRITE | 4K–64M | 1 GiB | RDMA | 线速带宽 + 小块延迟 |
| S4 | 两台主机 | DRAM → VRAM | WRITE | 4K–1M | 64 MiB | 单侧 GPU | 带宽 |
| S5 | 两台主机 | VRAM → VRAM | WRITE | 4K–1M | 64 MiB | GPUDirect RDMA，一致性校验 | 带宽 + 数据正确 |
| S6 | 两台主机 | DRAM → DRAM | WRITE | 64K–64M | 1 GiB | 200 次迭代 | 持续带宽 |

共同参数：后端 UCX；迭代 100 次（S6 为 200）、预热 10 次；块大于 1 MiB 时迭代数按 nixlbench 默认 `large_blk_iter_ftr=16` 缩减；跨节点 case 的网卡由 `DEVICE_LIST`（testNixl）或 `device_list` / `UCX_NET_DEVICES`（mtest）指定，默认全部 RDMA 网卡（UCX 多轨聚合）。

设计原则：小块看延迟，大块看带宽；READ 与 WRITE 各覆盖一个方向；至少一轮一致性校验，用来发现带宽正常但数据错误的链路或内存故障。

mtest 另有存储项 `nixl.storage`（不属于 S1-S6）：`posix` / `posix_read`（POSIX，DRAM）与 `gds` / `gds_read`（GDS，VRAM，O_DIRECT），4K–16M，1 GiB，读 case 带一致性校验。

## 拓扑

- 单机项每台主机独立运行（testNixl `MODE=all` 第一阶段并行，每个容器自起本地 etcd；mtest `scope: node`）。
- 跨节点项两两配对、依次运行（避免争抢网卡影响带宽，并共用一个 etcd）：主机按顺序 `(h1,h2)(h3,h4)…`，奇数台时 h1 再与最后一台配对；每对中前者为 initiator。mtest 还可用 `pairs: rack_adjacent / rack_half / cross_rack`。
- 一次运行包含两个 nixlbench 进程，经 etcd 汇合，先注册的为 rank 0（initiator），因此 target 晚 5 秒启动；第二个进程须在 60 秒内加入。

## 判定

一次运行（一个 case，跨节点时为一个 case × 一对主机）在以下情况判为失败：

1. initiator 退出码非 0（超时为 124），或没有结果表；
2. 日志出现一致性错误（`Consistency check failed`）或进程异常（段错误、abort）；
3. 设置了基线时（mtest `min_bw_gbps` / `max_lat_us`）：最大块带宽低于下限，或最小块延迟高于上限。

testNixl 另外检查日志中的 `error`、`fail`、`mismatch` 等关键字。

报告中每次运行给出最小块延迟和最大块带宽。

## 验收建议

| 项 | 建议判据 |
| --- | --- |
| S1 | 与同型号金样本主机相差不超过 10% |
| S3 / S6 | 达到网卡线速的 90% 以上（单网卡时，400 Gb/s ≈ 50 GB/s，800 Gb/s ≈ 100 GB/s；多轨聚合按网卡数累加） |
| S5 | 一致性校验通过；带宽与金样本相差不超过 10% |
| 4 KiB 延迟（S3） | 同机房 IB / RoCE 小于 10 µs |

GB300 的具体基线在金样本 rack 实测后填入 mtest `nixl.common.min_bw_gbps` / `max_lat_us`。已有实测参考见 [docs/nixl-benchmark.md](../../docs/nixl-benchmark.md#四结果解读)。
