"""Cross-checks of tests.yaml against cluster.yaml."""

from mtest.items import ITEMS, NEEDS_IMEX, known_selector
from mtest.render import unknown_vars
from mtest.selection import SelectionError, build_groups

POSITIVE_INTS = ("master_port_base", "mpi_ssh_port", "rendezvous_timeout", "run_timeout")


def enabled_items(tests):
    out = []
    for item in ITEMS:
        cfg = item.config(tests.data)
        if cfg and cfg.get("enabled") is True:
            out.append((item, cfg))
    return out


def check_tests(tests, cluster, problems):
    data = tests.data
    if data.get("version", 1) != 1:
        problems.warn("tests.yaml: version {} is not 1".format(data.get("version")))
    if data.get("image_pull", "registry") not in ("registry", "push", "skip"):
        problems.error("image_pull must be registry, push or skip")
    rt = tests.runtime
    for key in POSITIVE_INTS:
        if not isinstance(rt.get(key), int) or rt[key] <= 0:
            problems.error("runtime.{} must be a positive integer".format(key))
    poll = rt.get("poll_interval")
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        problems.error("runtime.poll_interval must be a positive number")
    if rt.get("group_order") not in ("serial", "parallel"):
        problems.error("runtime.group_order must be serial or parallel")
    if not isinstance(rt.get("container_args"), list):
        problems.error("runtime.container_args must be a list")
    bad = unknown_vars(tests.common_env)
    if bad:
        problems.error("common_env: unknown template variables {}".format(sorted(bad)))

    for item in ITEMS:
        cfg = item.config(data)
        if cfg is None:
            continue
        if not isinstance(cfg.get("enabled", False), bool):
            problems.error("{}: enabled must be true or false".format(item.id))
            continue
        if not cfg.get("enabled"):
            continue
        _check_item(tests, cluster, item, cfg, problems)

    enabled = {item.id for item, _ in enabled_items(tests)}
    if "imex" not in enabled:
        need = [i for i in NEEDS_IMEX if i in enabled]
        if need:
            problems.warn("{} use MNNVL but imex is not enabled; they rely on IMEX already being set up"
                          .format(", ".join(need)))
    if not enabled:
        problems.warn("tests.yaml: no item is enabled")

    for h in cluster.hosts:
        for s in h.skip:
            if not known_selector(s):
                problems.warn("{}: skip '{}' does not match any test item".format(h.name, s))


def _check_item(tests, cluster, item, cfg, problems):
    where = item.id
    if cfg.get("scope", "node") not in ("node", "rack", "group"):
        problems.error("{}: scope must be node, rack or group".format(where))
        return
    try:
        groups, _ = build_groups(cluster, item, cfg)
    except SelectionError as e:
        problems.error("{}: {}".format(where, e))
        return
    if not groups:
        problems.warn("{}: no hosts selected".format(where))
    if not tests.image_for(item, cfg):
        problems.error("{}: image '{}' is not set under images".format(where, cfg.get("image") or item.image))
    for field in ("env", "cmd", "args", "per_rank"):
        bad = unknown_vars(cfg.get(field))
        if bad:
            problems.error("{}.{}: unknown template variables {}".format(where, field, sorted(bad)))
    for key in (cfg.get("per_rank") or {}):
        if not str(key).isdigit():
            problems.error("{}.per_rank: key '{}' must be a rank number".format(where, key))
    if item.id == "superbench.pair" and groups and len(groups[0].hosts) != 2:
        problems.warn("superbench.pair: expected 2 hosts, got {}".format(len(groups[0].hosts)))
    if item.id == "sharp.scale_out":
        for g in groups:
            if len({h.rack for h in g.hosts}) < 2:
                problems.warn("sharp.scale_out: group {} has only one rack; IB SHARP needs at least 2".format(g.id))
    if item.section == "superbench" and cfg.get("profile", "gb300") != "gb300":
        problems.warn("{}: profile is '{}', GB300 configs need gb300".format(where, cfg.get("profile")))
    if item.id == "smoke" and cfg.get("mode", item.mode) not in ("per-node", "mpi", "delegate"):
        problems.error("smoke: mode must be per-node, mpi or delegate")
    if item.id == "superbench.disk":
        problems.warn("superbench.disk overwrites disks; it is implemented in a later phase (P10)")
