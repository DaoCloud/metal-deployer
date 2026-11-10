"""Built-in test items, in the order "mtest run" executes them."""


class Item:
    """One runnable test item.

    section/sub locate the config in tests.yaml (sub is None for single-section items).
    mode is how containers start: per-node (same entrypoint on every node, joined by RANK),
    mpi (sshd containers, mpirun from rank 0), or delegate (one control container that
    fans out by itself, such as SuperBench).
    batch means one invocation covers every group, as sb does for single-node configs.
    """

    def __init__(self, id, section, sub, mode, image, phase, title, batch=False):
        self.id = id
        self.title = title
        self.section = section
        self.sub = sub
        self.mode = mode
        self.image = image
        self.phase = phase
        self.batch = batch

    def config(self, tests_data):
        """Merged config: <section>.common (if any) deep-merged with the item section."""
        from mtest.util import deep_merge
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
    # S3/S4 run through "mtest nic" and "mtest net", not "mtest run"; listed for state and report.
    Item("nic", "nic", None, "host", "", "P3", "S3 网卡识别"),
    Item("network", "network", None, "host", "", "P3", "S4 网络配置"),
    Item("smoke", "smoke", None, "per-node", "sb_worker", "P2", "冒烟测试（smoke）"),
    Item("dcgm", "dcgm", None, "per-node", "dcgm", "P4", "S5 DCGM 诊断"),
    Item("superbench.single_node", "superbench", "single_node", "delegate", "sbcli", "P5", "S6 SuperBench 单机", batch=True),
    Item("superbench.disk", "superbench", "disk", "per-node", "sb_worker", "P10", "S6 SuperBench 磁盘"),
    Item("superbench.pair", "superbench", "pair", "delegate", "sbcli", "P5", "S6 SuperBench 双节点 IB"),
    Item("superbench.rack", "superbench", "rack", "delegate", "sbcli", "P5", "S6 SuperBench rack 内 IB"),
    Item("superbench.all", "superbench", "all", "delegate", "sbcli", "P5", "S6 SuperBench 全部节点 IB"),
    Item("imex", "imex", None, "mpi", "sb_worker", "P7", "S7 IMEX"),
    Item("sharp.scale_up", "sharp", "scale_up", "mpi", "sharp", "P7", "S7a NVLink SHARP（柜内）"),
    Item("sharp.scale_out", "sharp", "scale_out", "mpi", "sharp", "P7", "S7a IB SHARP（柜间）"),
    Item("moe.single", "moe", "single", "per-node", "moe_bench", "P8", "S8 MoE 单机（L1）"),
    Item("moe.rack", "moe", "rack", "per-node", "moe_bench", "P8", "S8 MoE rack 内（L2）"),
    Item("moe.cross_rack", "moe", "cross_rack", "per-node", "moe_bench", "P8", "S8 MoE 跨 rack（L3）"),
    Item("moe.all", "moe", "all", "per-node", "moe_bench", "P8", "S8 MoE 全部节点（L4）"),
    Item("moe.moonep", "moe", "moonep", "per-node", "moe_bench", "later", "S8 MoonEP"),
]

BY_ID = {item.id: item for item in ITEMS}

# Items that need a healthy IMEX domain (MNNVL) in the same run.
NEEDS_IMEX = ("sharp.scale_up", "sharp.scale_out", "moe.rack", "moe.cross_rack", "moe.all")


def matches(item_id, selector):
    """True if selector names the item or one of its prefixes (moe matches moe.rack)."""
    return item_id == selector or item_id.startswith(selector + ".")


def known_selector(selector):
    return any(matches(item.id, selector) for item in ITEMS if item.mode != "host")
