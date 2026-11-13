"""Cross-checks of tests.yaml against the hosts of network.yaml."""

from testkit.items import ITEMS, NEEDS_IMEX, known_selector
from testkit.render import unknown_vars
from testkit.selection import SelectionError, build_groups

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
    check_counters(tests, problems)
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
    if item.mode != "host" and not tests.image_for(item, cfg):
        problems.error("{}: image '{}' is not set under images".format(where, cfg.get("image") or item.image))
    for key in ("gate", "full_rack"):
        if cfg.get(key) is not None and not isinstance(cfg.get(key), bool):
            problems.error("{}: {} must be true or false".format(where, key))
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
    if item.id == "smoke" and cfg.get("mode", item.mode) not in ("per-node", "mpi", "delegate"):
        problems.error("smoke: mode must be per-node, mpi or delegate")
    if item.section == "sharp":
        from testkit.runners import SHARP_CASES, SHARP_OPS
        bad = [c for c in cfg.get("cases") or [] if c not in SHARP_CASES[item.id]]
        if bad:
            problems.error("{}: cases {} not in {}".format(where, bad, SHARP_CASES[item.id]))
        bad = [o for o in cfg.get("ops") or [] if o not in SHARP_OPS]
        if bad:
            problems.error("{}: ops {} not in {}".format(where, bad, SHARP_OPS))
        if not (cfg.get("worker_image") or tests.images.get("sb_worker")):
            problems.error("{}: images.sb_worker (or sharp.common.worker_image) is required".format(where))
    if item.section == "imex" and not tests.images.get("sb_worker"):
        problems.error("imex: images.sb_worker is required (T1/T2 containers)")
    if item.section == "superbench" and item.id != "superbench.disk":
        if not cfg.get("config"):
            problems.error("{}: config is required (e.g. config_gb300_multi_node.yaml)".format(where))
        if not tests.images.get("sb_worker"):
            problems.error("{}: images.sb_worker is required (worker image on every host)".format(where))
        if item.id not in ("superbench.single_node", "superbench.nccl_node"):
            for g in groups:
                if len(g.hosts) < 2:
                    problems.warn("{}: group {} has one host; multi-node benchmarks need at least 2".format(where, g.id))
    if cfg.get("timeout") is not None and (not isinstance(cfg["timeout"], int) or cfg["timeout"] <= 0):
        problems.error("{}: timeout must be a positive integer (seconds)".format(where))
    if item.id == "dcgm":
        if cfg.get("hostengine", "container") not in ("host", "container"):
            problems.error("dcgm.hostengine must be host or container")
        if cfg.get("level", 3) not in (1, 2, 3, 4):
            problems.error("dcgm.level must be 1, 2, 3 or 4")
        unknown = set(cfg.get("checks") or []) - {"ecc", "remapped_rows", "nvlink", "fabric_state"}
        if unknown:
            problems.error("dcgm.checks: unknown {}".format(sorted(unknown)))
    if item.id == "superbench.disk":
        problems.warn("superbench.disk overwrites disks; it is implemented in a later phase (P10)")
    if item.section == "nvlink":
        for g in groups:
            if len({h.rack for h in g.hosts}) > 1:
                problems.warn("{}: group {} spans racks; an NVLink domain is one rack".format(where, g.id))
            missing = [h.name for h in g.hosts if not h.mgmt]
            if missing:
                problems.error("{}: hosts without mgmt address: {}".format(where, ", ".join(missing[:5])))
        if cfg.get("outlier_action", "fail") not in ("fail", "warn"):
            problems.error("{}: outlier_action must be fail or warn".format(where))
        if item.id == "nvlink.fabric":
            from testkit.runners import NVLOOM_SUITES
            unknown = [s for s in cfg.get("suites") or [] if s not in NVLOOM_SUITES + ["rack-to-rack", "pairwise-tma",
                                                                                     "multicast-tma", "egm",
                                                                                     "rack-aware-all-to-one",
                                                                                     "egm-gpu-to-rack"]]
            if unknown:
                problems.warn("nvlink.fabric: suites {} are not known NVLoom suites".format(unknown))
    if item.section == "superbench" and cfg.get("ib_pairs") is not None:
        from testkit.ibpairs import PAIRINGS
        if cfg["ib_pairs"] not in PAIRINGS:
            problems.error("{}: ib_pairs must be one of {}".format(where, ", ".join(PAIRINGS)))
        elif cfg["ib_pairs"] == "cross_rack":
            for g in groups:
                if len({h.rack for h in g.hosts}) < 2:
                    problems.warn("{}: group {} has one rack; cross_rack pairs need at least 2".format(where, g.id))
        if cfg.get("cross_rack_rounds", "all") not in ("all", "first"):
            problems.error("{}: cross_rack_rounds must be all or first".format(where))
    if item.section == "superbench" and (cfg.get("outlier") or {}).get("action", "fail") not in ("fail", "warn"):
        problems.error("{}: outlier.action must be fail or warn".format(where))
    if item.id == "nvshmem.pair" and groups and len(groups[0].hosts) != 2:
        problems.warn("nvshmem.pair: expected 2 hosts, got {}".format(len(groups[0].hosts)))
    if item.section == "nixl":
        check_nixl(item, cfg, groups, problems)


def check_nixl(item, cfg, groups, problems):
    from testkit.runners import NIXL_CASES, NIXL_KIND, NIXL_PAIRINGS
    kind = NIXL_KIND[item.id]
    for name in cfg.get("cases") or []:
        if name not in NIXL_CASES:
            problems.error("{}: unknown case {} (known: {})".format(item.id, name, ", ".join(sorted(NIXL_CASES))))
        elif NIXL_CASES[name]["kind"] != kind:
            problems.error("{}: case {} is a {} case, not {}".format(item.id, name, NIXL_CASES[name]["kind"], kind))
    for key in ("min_bw_gbps", "max_lat_us", "case_args"):
        for name in (cfg.get(key) or {}):
            if name not in NIXL_CASES:
                problems.warn("{}: {} names unknown case {}".format(item.id, key, name))
    gpus = cfg.get("gpus")
    if gpus is not None and (not isinstance(gpus, list) or len(gpus) != 2 or not all(isinstance(g, int) for g in gpus)):
        problems.error("{}: gpus must be two GPU indexes, e.g. [0, 1]".format(item.id))
    if kind == "pair":
        if cfg.get("pairs", "sequential") not in NIXL_PAIRINGS:
            problems.error("{}: pairs must be one of {}".format(item.id, ", ".join(NIXL_PAIRINGS)))
        for g in groups:
            if len(g.hosts) < 2:
                problems.warn("{}: group {} has one host; pair cases need 2".format(item.id, g.id))
    else:
        for g in groups:
            if len(g.hosts) > 1:
                problems.warn("{}: group {} has {} hosts but runs on its rank 0 host only; use scope: node".format(
                    item.id, g.id, len(g.hosts)))
    if kind == "storage" and not cfg.get("filepath"):
        problems.error("{}: filepath is not set (a directory on the host)".format(item.id))
    if kind != "storage":
        missing = [h.name for g in groups for h in g.hosts if not h.mgmt]
        if missing:
            problems.error("{}: hosts without mgmt address (etcd and nixlbench ranks use it): {}".format(
                item.id, ", ".join(missing[:5])))


def check_counters(tests, problems):
    cfg = tests.data.get("counters")
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        problems.error("counters must be a mapping")
        return
    for s in cfg.get("items") or []:
        if not known_selector(s):
            problems.warn("counters.items: '{}' does not match any test item".format(s))
    if not isinstance(cfg.get("max_increase", 0), int) or cfg.get("max_increase", 0) < 0:
        problems.error("counters.max_increase must be a non-negative integer")
