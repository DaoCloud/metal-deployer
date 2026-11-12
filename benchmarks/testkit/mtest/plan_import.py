"""mtest import: read the IP plan (.xlsx or .csv) into the hosts section of cluster.yaml.

Only the hosts section is rewritten; ssh, network and comments stay. tags, env, skip and
gpus of hosts already in cluster.yaml are kept. Column names follow the GB300 IP plan
(hostname, rack, tray, sn, lom_ip, bf3_mgt_ip, bf3_mgt_pci, ib_rail1_pci, ...); hardware-neutral
names (mgmt_ip, storage_ip, rail1_pci, ...) and the cluster.yaml field names (name, ssh, mgmt,
mgmt_pci, ...) work as well.
"""

import csv
import os
import re

import yaml

from mtest import scaffold
from mtest.config import load_cluster
from mtest.util import ConfigError, Problems, load_yaml

# plan column -> cluster.yaml host field
COLUMNS = {
    "hostname": "name", "name": "name", "rack": "rack", "tray": "tray", "sn": "serial", "serial": "serial",
    "lom_ip": "ssh", "ssh": "ssh", "lom_gateway": "lom_gateway",
    "bf3_mgt_ip": "mgmt", "mgmt_ip": "mgmt", "mgmt": "mgmt", "bf3_mgt_gateway": "mgmt_gateway",
    "mgmt_gateway": "mgmt_gateway",
    "bf3_mgt_pci": "mgmt_pci", "mgmt_pci": "mgmt_pci", "bf3_mgt_mac": "mgmt_mac", "mgmt_mac": "mgmt_mac",
    "bf3_storage_ip": "storage", "storage_ip": "storage", "storage": "storage", "bf3_storage_gateway": "storage_gateway",
    "storage_gateway": "storage_gateway", "bf3_storage_routes": "storage_routes", "storage_routes": "storage_routes",
    "bf3_storage_pci": "storage_pci", "storage_pci": "storage_pci", "bf3_storage_mac": "storage_mac",
    "storage_mac": "storage_mac", "ib_mtu": "ib_mtu",
}
RAIL_RE = re.compile(r"^(?:ib_)?rail(\d+)_(pci|guid|ip)$")
KEEP = ("tags", "env", "skip", "gpus")
ORDER = ("name", "rack", "tray", "serial", "ssh", "mgmt", "mgmt_gateway", "mgmt_pci", "mgmt_mac", "storage",
         "storage_gateway", "storage_routes", "storage_pci", "storage_mac", "lom_gateway", "ib", "ib_pci", "ib_guid",
         "ib_mtu") + KEEP

NEW_NETWORK = """network:
  profile: gb300                  # gb300: BF3 port 0/1 = mgmt/storage, 4x CX8 on IB; custom: see mtest init
  ib_mode: {enforce: true, reboot: ask}      # reboot: ask / auto / never
  lom: {table: 100, priority: 100}           # policy routing table for replies from the LOM address
  # Gateways and storage routes come from the IP plan (per host). BF3 MTU is left unchanged.
  mgmt: {default_route: true}
  ipoib: {pkey: "0x7fff"}         # IPoIB MTU per host: ib_mtu column
  require_pci: true               # every port is found by the PCI address in the plan
  match_by: mac
  rollback_seconds: 120
  verify: {ping_gateway: true, ping_peer_in_rack: true}

"""


class ImportError_(Exception):
    pass


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def read_rows(path, sheet=None):
    """Rows of the plan as dicts with lower-case column names."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise ImportError_("reading .xlsx needs openpyxl (pip install openpyxl), or save the sheet as CSV")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if sheet:
            if sheet not in wb.sheetnames:
                raise ImportError_("sheet '{}' not found; sheets: {}".format(sheet, ", ".join(wb.sheetnames)))
            ws = wb[sheet]
        else:
            ws = wb["hosts"] if "hosts" in wb.sheetnames else wb.worksheets[0]
        rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [[_cell(c) for c in r] for r in csv.reader(f)]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ImportError_("{}: no rows".format(path))
    head = [h.lower() for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:]]


def to_hosts(rows):
    """(hosts, unknown_columns)"""
    hosts = []
    unknown = set()
    n_rails = max([int(m.group(1)) for m in (RAIL_RE.match(c) for c in (rows[0] if rows else {})) if m] or [0])
    for row in rows:
        h = {}
        rails = {}
        for col, val in row.items():
            if not col:
                continue
            m = RAIL_RE.match(col)
            if m:
                rails.setdefault(m.group(2), {})[int(m.group(1))] = val
                continue
            if col not in COLUMNS:
                unknown.add(col)
                continue
            if val != "":
                h[COLUMNS[col]] = val
        if not h.get("name"):
            continue
        rack = h.get("rack", "")
        h["rack"] = "R" + rack if rack.isdigit() else rack
        for key in ("tray", "ib_mtu"):
            if str(h.get(key, "")).isdigit():
                h[key] = int(h[key])
        if "ssh" in h:
            h["ssh"] = h["ssh"].split("/")[0]
        if "storage_routes" in h:
            h["storage_routes"] = [r for r in re.split(r"[,;\s]+", h["storage_routes"]) if r]
        for kind, field in (("pci", "ib_pci"), ("guid", "ib_guid"), ("ip", "ib")):
            vals = rails.get(kind) or {}
            if any(vals.values()):
                h[field] = [vals.get(i, "") for i in range(1, n_rails + 1)]
        hosts.append(h)
    return hosts, sorted(unknown - {"node_no", "bmc_ip", "bmc_gateway", "lom_pci", "lom_mac", "bf3_oob_ip", "remark"})


def hosts_text(hosts):
    lines = ["hosts:"]
    for h in hosts:
        entry = {k: h[k] for k in ORDER if k in h and h[k] not in ("", None, [], {})}
        text = yaml.safe_dump([entry], sort_keys=False, default_flow_style=None, allow_unicode=True, width=200)
        lines.extend("  " + line for line in text.rstrip("\n").splitlines())
    return "\n".join(lines) + "\n"


def replace_hosts(text, block):
    """Replace the top-level hosts: section of a cluster.yaml text, keep everything else."""
    lines = text.splitlines(True)
    start = next((i for i, l in enumerate(lines) if re.match(r"^hosts\s*:", l)), None)
    if start is None:
        return text.rstrip("\n") + "\n\n" + block
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_][\w-]*\s*:", lines[i])), len(lines))
    tail = "".join(lines[end:])
    return "".join(lines[:start]) + block + ("\n" + tail if tail else "")


def run_import(plan_path, cluster_path, sheet=None, dry_run=False, out=print):
    if not os.path.isfile(plan_path):
        out("error: {} not found".format(plan_path))
        return 1
    try:
        hosts, unknown = to_hosts(read_rows(plan_path, sheet))
    except ImportError_ as e:
        out("error: " + str(e))
        return 1
    if not hosts:
        out("error: no host rows (a hostname column is required)")
        return 1
    existing = {}
    if os.path.isfile(cluster_path):
        try:
            data = load_yaml(cluster_path)
        except ConfigError as e:
            out("error: " + str(e))
            return 1
        existing = {h.get("name"): h for h in data.get("hosts") or [] if isinstance(h, dict)}
        with open(cluster_path, encoding="utf-8") as f:
            base = f.read()
    else:
        base = scaffold.cluster_text(1, 1)
        base = re.sub(r"(?ms)^network:.*?(?=^defaults:)", NEW_NETWORK, base)
    for h in hosts:
        old = existing.get(h["name"]) or {}
        for key in KEEP:
            if key in old:
                h[key] = old[key]
    if not any("golden" in (h.get("tags") or []) for h in hosts):
        hosts[0]["tags"] = list(hosts[0].get("tags") or []) + ["golden"]
        out("note: no golden host; tagged {} as golden".format(hosts[0]["name"]))
    names = [h["name"] for h in hosts]
    added = [n for n in names if n not in existing]
    removed = [n for n in existing if n not in names]
    text = replace_hosts(base, hosts_text(hosts))
    racks = sorted({h["rack"] for h in hosts})
    out("{}: {} hosts, racks {}; {} new, {} removed, {} updated".format(
        os.path.basename(plan_path), len(hosts), ", ".join(racks), len(added), len(removed),
        len(names) - len(added)))
    for field in ("mgmt_pci", "storage_pci", "ib_pci", "ib_mtu"):
        missing = [h["name"] for h in hosts if not h.get(field) or (isinstance(h[field], list) and not all(h[field]))]
        if missing:
            out("  {:<12} empty for {} hosts (e.g. {})".format(field, len(missing), missing[0]))
    if unknown:
        out("  ignored columns: " + ", ".join(unknown))
    if removed:
        out("  removed from cluster.yaml: " + ", ".join(removed))
    tmp = cluster_path + ".import.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    problems = Problems()
    try:
        load_cluster(tmp, problems)
    except ConfigError as e:
        problems.error(str(e))
    for msg in problems.errors:
        out("error: " + msg)
    if dry_run or not problems.ok:
        os.unlink(tmp)
        if not problems.ok:
            out("cluster.yaml not changed: fix the plan and import again")
            return 1
        out("dry run: cluster.yaml not changed")
        return 0
    if os.path.isfile(cluster_path):
        os.replace(cluster_path, cluster_path + ".bak")
    os.replace(tmp, cluster_path)
    out("wrote {}{}".format(cluster_path, " (previous: {}.bak)".format(cluster_path)
                            if os.path.isfile(cluster_path + ".bak") else ""))
    return 0
