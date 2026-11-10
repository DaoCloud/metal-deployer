"""Build and print what "mtest run" would do, without touching any host."""

from mtest.checks import enabled_items
from mtest.items import matches
from mtest.render import node_cmd, node_env, node_vars
from mtest.selection import build_groups
from mtest.state import load_nic

STAGE = {"dcgm": "S5", "superbench": "S6", "imex": "S7", "sharp": "S7a", "moe": "S8"}


class NodePlan:

    def __init__(self, group, rank, host, env, cmd):
        self.group = group
        self.rank = rank
        self.host = host
        self.env = env
        self.cmd = cmd


class ItemPlan:

    def __init__(self, item, cfg, image, groups, notes, nodes):
        self.item = item
        self.cfg = cfg
        self.image = image
        self.groups = groups
        self.notes = notes
        self.nodes = nodes


def build_plan(cluster, tests, selectors=None, only=None, exclude=None, run_id="plan"):
    """Plans of enabled items in run order. selectors limit items by id or prefix."""
    base = tests.runtime["master_port_base"]
    plans = []
    nic_cache = {}
    for item, cfg in enabled_items(tests):
        if selectors and not any(matches(item.id, s) for s in selectors):
            continue
        groups, notes = build_groups(cluster, item, cfg, only=only, exclude=exclude)
        nodes = []
        for g in groups:
            port = base + g.index
            for rank, host in enumerate(g.hosts):
                if host.name not in nic_cache:
                    nic_cache[host.name] = load_nic(tests.results_dir, host.name)
                variables = node_vars(run_id, g, rank, host, port, nic_cache[host.name])
                nodes.append(NodePlan(g, rank, host, node_env(tests.common_env, cfg, rank, host, variables),
                                      node_cmd(cfg, rank, variables)))
        plans.append(ItemPlan(item, cfg, tests.image_for(item, cfg), groups, notes, nodes))
    return plans


def _short(name):
    return name.split("-", 1)[1] if name.startswith("gb300-") else name


def _cmd_text(cmd):
    return " ".join(cmd) if cmd else None


def format_plan(plans, base_port, verbose=False):
    lines = []
    pending_nic = False
    for p in plans:
        item = p.item
        lines.append("== {} [{}]  scope={}  mode={}{}".format(
            item.id, STAGE.get(item.section, "?"), p.cfg.get("scope", "node"), item.mode,
            "  (one invocation for all groups)" if item.batch else ""))
        lines.append("   image: {}".format(p.image or "(not set)"))
        if not any(n.cmd for n in p.nodes):
            lines.append("   command: built by mtest ({})".format(item.phase))
        for note in p.notes:
            lines.append("   note: {}".format(note))
        by_group = {}
        for n in p.nodes:
            by_group.setdefault(n.group.id, []).append(n)
            if any("<S3:" in v for v in n.env.values()) or (n.cmd and any("<S3:" in c for c in n.cmd)):
                pending_nic = True
        scope = p.cfg.get("scope", "node")
        if scope == "node" and not verbose:
            names = [g.id for g in p.groups]
            span = names[0] if len(names) == 1 else "{} .. {}".format(names[0], names[-1]) if names else "-"
            lines.append("   {} groups, one per host: {}".format(len(names), span))
            if p.nodes:
                _node_lines(lines, p.nodes[0], "   e.g. ")
            lines.append("")
            continue
        for g in p.groups:
            members = by_group[g.id]
            lines.append("   group {}: {} hosts, master {} {}:{}".format(
                g.id, len(g.hosts), g.master.name, g.master.mgmt_ip, base_port + g.index))
            if verbose:
                for n in members:
                    _node_lines(lines, n, "     ")
                continue
            ranks = ", ".join("{} {}".format(n.rank, _short(n.host.name)) for n in members)
            lines.append("     ranks: {}".format(ranks))
            _node_lines(lines, members[0], "     e.g. ")
            varying = sorted(k for k in members[0].env if len({n.env.get(k) for n in members}) > 1)
            if varying:
                lines.append("     differs per node: {}".format(", ".join(varying)))
        lines.append("")
    if pending_nic:
        lines.append("note: <S3:...> values are filled after 'mtest nic discover' has run on the hosts.")
    return "\n".join(lines).rstrip() + "\n"


def _node_lines(lines, n, prefix):
    lines.append("{}{} (rank {})".format(prefix, n.host.name, n.rank))
    pad = " " * len(prefix)
    if n.env:
        lines.append("{}  env: {}".format(pad, " ".join("{}={}".format(k, v) for k, v in n.env.items())))
    if n.cmd:
        lines.append("{}  cmd: {}".format(pad, _cmd_text(n.cmd)))
