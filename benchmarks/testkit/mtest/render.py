"""Template variables and the per-node environment / command of a test item."""

import re

from mtest.util import env_str

VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

GROUP_VARS = {"run_id", "group_id", "node_rank", "nnodes", "master_addr", "master_port", "node_list", "rack_list"}
HOST_VARS = {"hostname", "rack", "tray", "gpus", "mgmt_ip", "storage_ip", "ib0_ip", "ib1_ip", "ib2_ip", "ib3_ip"}
# Filled from "mtest nic discover" results; until then rendered as a visible placeholder.
NIC_VARS = {"mgmt_if", "ib_hcas", "ib_ucx_devs"}
KNOWN_VARS = GROUP_VARS | HOST_VARS | NIC_VARS


def unknown_vars(value):
    """Template variables used in value (str, list or dict) that mtest does not know."""
    found = set()
    if isinstance(value, str):
        found.update(VAR_RE.findall(value))
    elif isinstance(value, list):
        for v in value:
            found |= unknown_vars(v)
    elif isinstance(value, dict):
        for v in value.values():
            found |= unknown_vars(v)
    return found - KNOWN_VARS


def render(value, variables):
    """Replace {name} in a string; unknown names are left as they are."""
    def sub(m):
        name = m.group(1)
        if name in variables:
            return str(variables[name])
        if name in NIC_VARS:
            return "<S3:{}>".format(name)
        return m.group(0)
    return VAR_RE.sub(sub, env_str(value))


def nic_vars(nic):
    if not nic:
        return {}
    devs = nic.get("ib_devs") or []
    out = {"ib_hcas": ",".join(devs), "ib_ucx_devs": ",".join(d + ":1" for d in devs)}
    if nic.get("mgmt_if"):
        out["mgmt_if"] = nic["mgmt_if"]
    return out


def node_vars(run_id, group, rank, host, master_port, nic=None):
    master = group.master
    out = {
        "run_id": run_id,
        "group_id": group.id,
        "node_rank": rank,
        "nnodes": len(group.hosts),
        "master_addr": master.mgmt_ip,
        "master_port": master_port,
        "node_list": ",".join(h.mgmt_ip for h in group.hosts),
        "rack_list": ",".join(h.rack for h in group.hosts),
        "hostname": host.name,
        "rack": host.rack,
        "tray": host.tray,
        "gpus": host.gpus,
        "mgmt_ip": host.mgmt_ip,
        "storage_ip": str(host.storage.ip) if host.storage else "",
    }
    for i in range(4):
        out["ib{}_ip".format(i)] = str(host.ib[i].ip) if i < len(host.ib) and host.ib[i] else ""
    out.update(nic_vars(nic))
    return out


def per_rank(cfg, rank):
    table = cfg.get("per_rank") or {}
    entry = table.get(str(rank), table.get(rank)) if isinstance(table, dict) else None
    return entry if isinstance(entry, dict) else {}


def node_env(common_env, cfg, rank, host, variables):
    """Merge the env layers (later wins) and render them for one node."""
    layers = [common_env or {}, cfg.get("env") or {}, per_rank(cfg, rank).get("env") or {}, host.env]
    merged = {}
    for layer in layers:
        for key, value in layer.items():
            merged[str(key)] = value
    return {k: render(v, variables) for k, v in merged.items()}


def node_cmd(cfg, rank, variables):
    """cmd + args + per_rank args_append, rendered; None if the item has no cmd in tests.yaml."""
    cmd = cfg.get("cmd")
    if not cmd:
        return None
    parts = list(cmd) + list(cfg.get("args") or []) + list(per_rank(cfg, rank).get("args_append") or [])
    return [render(p, variables) for p in parts]
