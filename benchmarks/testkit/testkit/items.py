"""Built-in test items, in the order "testkit run" executes them."""


class Item:
    """One runnable test item.

    section/sub locate the config in tests.yaml (sub is None for single-section items).
    mode is how containers start: per-node (same entrypoint on every node, joined by RANK),
    mpi (sshd containers, mpirun from rank 0), delegate (one control container that
    fans out by itself, such as SuperBench), or host (no container: commands run on the
    hosts over ssh). cli items are run by their own command ("testkit nic").
    batch means one invocation covers every group, as sb does for single-node configs.
    layer is the acceptance layer (L0 link state ... L4 NCCL, L5 MoE); gate is the
    default of tests.yaml "gate": a gated item that does not pass stops the run.
    full_rack: a rack group only runs when no host of the rack was left out after an earlier failure.
    """

    def __init__(self, id, section, sub, mode, image, phase, title, batch=False, layer="", gate=False,
                 full_rack=False):
        self.id = id
        self.title = title
        self.section = section
        self.sub = sub
        self.mode = mode
        self.image = image
        self.phase = phase
        self.batch = batch
        self.layer = layer
        self.gate = gate
        self.full_rack = full_rack

    def config(self, tests_data):
        """Merged config: <section>.common (if any) deep-merged with the item section."""
        from testkit.util import deep_merge
        section = tests_data.get(self.section)
        if not isinstance(section, dict):
            return None
        if self.sub is None:
            return dict(section)
        item = section.get(self.sub)
        if not isinstance(item, dict):
            return None
        return deep_merge(section.get("common") or {}, item)


ITEMS = [
    # S3 runs through "testkit nic", not "testkit run"; listed for state and report.
    Item("nic", "nic", None, "cli", "", "P3", "S3 网卡识别"),
    Item("smoke", "smoke", None, "per-node", "sb_worker", "P2", "冒烟测试（smoke）"),
    # L0: static link state and error counters of every NVLink and IB port, read-only.
    Item("links", "links", None, "host", "", "P11", "L0 链路状态与错误计数", layer="L0", gate=True),
    # L1: one host at a time.
    Item("dcgm", "dcgm", None, "per-node", "dcgm", "P4", "S5 DCGM 诊断", layer="L1", gate=True),
    Item("superbench.single_node", "superbench", "single_node", "delegate", "sbcli", "P5", "S6 SuperBench 单机",
         batch=True, layer="L1", gate=True),
    Item("superbench.disk", "superbench", "disk", "per-node", "sb_worker", "P10", "S6 SuperBench 磁盘", layer="L1"),
    # L2: RDMA point to point, same rail on both ends: inside each rack first, then rack to rack.
    Item("superbench.pair", "superbench", "pair", "delegate", "sbcli", "P5", "S6 RDMA 双节点冒烟", layer="L2",
         gate=True),
    Item("superbench.rack", "superbench", "rack", "delegate", "sbcli", "P5", "S6 RDMA 柜内两两（同 rail）",
         layer="L2", gate=True),
    Item("superbench.cross_rack", "superbench", "cross_rack", "delegate", "sbcli", "P5", "S6 RDMA 跨柜两两（同 rail）",
         layer="L2", gate=True),
    # L3: NVLink inside each rack: IMEX, every GPU pair, then the fabric under concurrent traffic.
    Item("imex", "imex", None, "mpi", "sb_worker", "P7", "S7 IMEX", layer="L3", gate=True, full_rack=True),
    Item("nvlink.pairs", "nvlink", "pairs", "host", "", "P11", "S7b NVLink GPU 对（mnnvbandwidth）", layer="L3",
         gate=True, full_rack=True),
    Item("nvlink.fabric", "nvlink", "fabric", "host", "", "P11", "S7b NVLink Fabric 并发（NVLoom）", layer="L3",
         gate=True, full_rack=True),
    # L4: NCCL collectives (whole-system throughput): one tray, one rack over MNNVL, all racks, IB SHARP.
    Item("superbench.nccl_node", "superbench", "nccl_node", "delegate", "sbcli", "P5", "NCCL 单机（节点内 NVLink）",
         batch=True, layer="L4"),
    Item("superbench.nvl_domain", "superbench", "nvl_domain", "delegate", "sbcli", "P7", "NCCL 柜内 MNNVL（含 NVLS）",
         layer="L4", full_rack=True),
    Item("superbench.all", "superbench", "all", "delegate", "sbcli", "P5", "NCCL 跨柜全量（柜内 NVLink + 柜间 IB）",
         layer="L4"),
    Item("sharp.scale_out", "sharp", "scale_out", "delegate", "sbcli", "P7", "NCCL 跨柜 IB SHARP", layer="L4"),
    Item("nvshmem.pair", "nvshmem", "pair", "mpi", "moe_bench", "P11", "NVSHMEM 点对点（可选）", layer="L4"),
    Item("nvshmem.rack", "nvshmem", "rack", "mpi", "moe_bench", "P11", "NVSHMEM rack 内 alltoall（可选）",
         layer="L4", full_rack=True),
    # NIXL (KV-cache style transfers, optional): GPU to GPU in one host, RDMA between hosts, storage.
    Item("nixl.node", "nixl", "node", "mpi", "nixl_benchmark", "P12", "NIXL 单机 GPU↔GPU（可选）", layer="L4"),
    Item("nixl.pair", "nixl", "pair", "mpi", "nixl_benchmark", "P12", "NIXL 跨节点 RDMA（可选）", layer="L4"),
    Item("nixl.storage", "nixl", "storage", "mpi", "nixl_benchmark", "P12", "NIXL 存储 POSIX/GDS（可选）",
         layer="L4"),
    Item("superbench.soak", "superbench", "soak", "delegate", "sbcli", "P5", "NCCL 长稳（跨柜 allreduce）", layer="L4"),
    # L5: MoE workloads.
    Item("moe.single", "moe", "single", "per-node", "moe_bench", "P8", "S8 MoE 单机（第 1 档）", layer="L5"),
    Item("moe.rack", "moe", "rack", "per-node", "moe_bench", "P8", "S8 MoE rack 内（第 2 档）", layer="L5", full_rack=True),
    Item("moe.cross_rack", "moe", "cross_rack", "per-node", "moe_bench", "P8", "S8 MoE 跨 rack（第 3 档）", layer="L5"),
    Item("moe.all", "moe", "all", "per-node", "moe_bench", "P8", "S8 MoE 全部节点（第 4 档）", layer="L5"),
    Item("moe.moonep", "moe", "moonep", "per-node", "moe_bench", "later", "S8 MoonEP", layer="L5"),
]

BY_ID = {item.id: item for item in ITEMS}

# Items that need a healthy IMEX domain (MNNVL) in the same run.
NEEDS_IMEX = ("nvlink.pairs", "nvlink.fabric", "superbench.nvl_domain", "superbench.all", "superbench.soak",
              "sharp.scale_out", "nvshmem.pair", "nvshmem.rack", "moe.rack", "moe.cross_rack", "moe.all")


def matches(item_id, selector):
    """True if selector names the item or one of its prefixes (moe matches moe.rack)."""
    return item_id == selector or item_id.startswith(selector + ".")


def known_selector(selector):
    return any(matches(item.id, selector) for item in ITEMS if item.mode != "cli")


def gate_of(item, cfg):
    """True when a failure of this item stops the run (tests.yaml "gate", default per item)."""
    value = (cfg or {}).get("gate")
    return item.gate if value is None else bool(value)


def full_rack_of(item, cfg):
    value = (cfg or {}).get("full_rack")
    return item.full_rack if value is None else bool(value)
