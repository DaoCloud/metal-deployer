"""NVLink / IB link state and error counters (node_scripts/link_counters.py).

Used by the L0 item "links" (state checks) and by the executor around L1-L4 items, which
takes a snapshot before and after an item and fails hosts whose error counters went up.
"""

import re

# NVLink counters whose increase is expected or not an error: FEC histogram bins, BER estimates,
# raw (pre-FEC) symbol errors.
NVLINK_IGNORE = [r"\bfec\b", r"\bber\b", r"\bsymbol\b"]
# IB port counters (/sys/class/infiniband/<dev>/ports/<n>/counters) that must not go up.
IB_WATCH = ["symbol_error", "link_error_recovery", "link_downed", "port_rcv_errors",
            "port_rcv_remote_physical_errors", "port_rcv_switch_relay_errors", "port_xmit_discards",
            "local_link_integrity_errors", "excessive_buffer_overrun_errors", "port_rcv_constraint_errors",
            "port_xmit_constraint_errors", "VL15_dropped"]
COUNTER_DEFAULTS = {"nvlink_ignore": NVLINK_IGNORE, "ib_watch": IB_WATCH, "max_increase": 0}
# Kernel messages that mean a hardware problem (matched case-insensitively); kernel_ignore removes lines again.
KERNEL_PATTERNS = [r"NVRM: Xid", r"fallen off the bus", r"nvswitch.*(error|fatal)",
                   r"mlx5_core.*(error|fail|timeout|link down|health|fatal|unrecover)",
                   r"AER: .*(uncorrected|fatal)", r"infiniband.*(error|fail)"]
COUNTER_DEFAULTS.update({"kernel_patterns": KERNEL_PATTERNS, "kernel_ignore": []})


def kernel_hits(snap, cfg=None):
    """Kernel lines of a snapshot that match kernel_patterns and not kernel_ignore."""
    cfg = dict(COUNTER_DEFAULTS, **(cfg or {}))
    pats = [re.compile(p, re.I) for p in cfg.get("kernel_patterns") or []]
    ign = [re.compile(p, re.I) for p in cfg.get("kernel_ignore") or []]
    lines = ((snap or {}).get("kernel") or {}).get("lines") or []
    return [l for l in lines if any(p.search(l) for p in pats) and not any(p.search(l) for p in ign)]


def _leaves(doc, path=""):
    """(key path, value) of a JSON document; {"values": [...]} leaves (mlxlink) become the joined values."""
    if isinstance(doc, dict):
        if "values" in doc and isinstance(doc["values"], list):
            yield path, ",".join(str(v) for v in doc["values"])
            return
        for k, v in doc.items():
            for item in _leaves(v, "{}/{}".format(path, k) if path else str(k)):
                yield item
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            for item in _leaves(v, "{}/{}".format(path, i)):
                yield item
    else:
        yield path, doc


def _num(text):
    try:
        return float(str(text).strip().split(",")[0].split()[0])
    except (ValueError, IndexError):
        return None


MLXLINK_FIELDS = {"effective_errors": "effective physical errors", "effective_ber": "effective physical ber",
                  "raw_ber": "raw physical ber", "link_down": "link down counter",
                  "error_recovery": "link error recovery counter", "temperature": "temperature",
                  "vendor": "vendor name", "part": "vendor part number", "serial": "vendor serial number",
                  "fw": "firmware version"}


def parse_mlxlink(doc):
    """{field: value} of one port's mlxlink --json output (fields of MLXLINK_FIELDS), or {"error"}."""
    if not isinstance(doc, dict):
        return {"error": "no output"}
    if doc.get("error") and len(doc) == 1:
        return {"error": str(doc["error"])}
    out = {}
    for path, value in _leaves(doc):
        key = path.split("/")[-1].lower()
        for field, name in MLXLINK_FIELDS.items():
            if field not in out and key.startswith(name):
                out[field] = value
    return out


def parse_gpus(text):
    """{index: bus_id} from nvidia-smi --query-gpu=index,pci.bus_id,serial."""
    out = {}
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            out[int(parts[0])] = parts[1]
    return out


def parse_nvlink_status(text):
    """{gpu: {link: "50 GB/s" | "<inactive>"}} from nvidia-smi nvlink -s."""
    out = {}
    gpu = None
    for line in (text or "").splitlines():
        m = re.match(r"^GPU (\d+):", line)
        if m:
            gpu = int(m.group(1))
            out[gpu] = {}
            continue
        m = re.match(r"^\s+Link (\d+):\s*(.*)$", line)
        if m and gpu is not None:
            out[gpu][int(m.group(1))] = m.group(2).strip()
    return out


def link_active(state):
    return bool(state) and "inactive" not in state.lower() and bool(re.search(r"\d", state))


def link_speed(state):
    m = re.search(r"([\d.]+)\s*GB/s", state or "")
    return float(m.group(1)) if m else None


def _int(value):
    value = value.strip()
    return int(value) if re.match(r"^-?\d+$", value) else None


def parse_nvlink_errors(text):
    """{gpu: {link: {counter: int}}} from nvidia-smi nvlink -e.

    Accepts both layouts: "Link 0: CRC Errors: 0" on one line, and a "Link 0:" header followed
    by indented "Name: value" lines. Non-integer values (BER estimates) are skipped.
    """
    out = {}
    gpu = link = None
    for line in (text or "").splitlines():
        m = re.match(r"^GPU (\d+):", line)
        if m:
            gpu, link = int(m.group(1)), None
            out[gpu] = {}
            continue
        if gpu is None:
            continue
        m = re.match(r"^\s+Link (\d+):\s*(.*)$", line)
        if m:
            link = int(m.group(1))
            out[gpu].setdefault(link, {})
            rest = m.group(2)
            if ":" in rest:
                name, _, value = rest.rpartition(":")
                v = _int(value)
                if v is not None:
                    out[gpu][link][name.strip()] = v
            continue
        m = re.match(r"^\s+(.+?):\s*(\S+)\s*$", line)
        if m and link is not None:
            v = _int(m.group(2))
            if v is not None:
                out[gpu][link][m.group(1).strip()] = v
    return out


def parse_p2p(text):
    """(labels, rows) from nvidia-smi topo -p2p n; rows[i][j] is "X", "OK", "NS", ..."""
    labels, rows = [], []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if not labels and all(re.match(r"^GPU\d+$", p) for p in parts):
            labels = parts
            continue
        if labels and re.match(r"^GPU\d+$", parts[0]) and len(parts) == len(labels) + 1:
            rows.append(parts[1:])
    return labels, rows


def ib_rate(text):
    m = re.match(r"^\s*([\d.]+)", text or "")
    return float(m.group(1)) if m else None


def rail_names(nic):
    """{ibdev: "rail N"} from the S3 record (testkit nic discover), if there is one."""
    out = {}
    for r in (nic or {}).get("rails") or []:
        if isinstance(r, dict) and r.get("ibdev"):
            out[r["ibdev"]] = "rail {}".format(r.get("rail"))
        elif isinstance(r, (list, tuple)) and len(r) >= 3 and r[2]:
            out[r[2]] = "rail {}".format(r[0])
    return out


def flatten(snap, cfg=None):
    """{"GPU0 link 3: CRC Errors": value, "mlx5_0/1: symbol_error": value} of watched counters,
    plus {name: value} of ignored ones (reported, never judged)."""
    cfg = dict(COUNTER_DEFAULTS, **(cfg or {}))
    ignore = [re.compile(p, re.I) for p in cfg.get("nvlink_ignore") or []]
    watched, ignored = {}, {}
    smi = (snap or {}).get("smi") or {}
    for gpu, links in parse_nvlink_errors(smi.get("nvlink_errors")).items():
        for link, counters in links.items():
            for name, value in counters.items():
                key = "GPU{} link {}: {}".format(gpu, link, name)
                (ignored if any(p.search(name) for p in ignore) else watched)[key] = value
    watch_ib = set(cfg.get("ib_watch") or [])
    for port, info in ((snap or {}).get("ib") or {}).items():
        if info.get("link_layer", "").lower() != "infiniband":
            continue
        for name, value in (info.get("counters") or {}).items():
            key = "{}: {}".format(port, name)
            (watched if name in watch_ib else ignored)[key] = value
    return watched, ignored


def delta(before, after, cfg=None):
    """Compare two snapshots. Returns {"increased": [(key, before, after)], "reset": [...],
    "ignored": [(key, before, after)], "state": [changes of NVLink/IB port state]}."""
    cfg = dict(COUNTER_DEFAULTS, **(cfg or {}))
    limit = int(cfg.get("max_increase") or 0)
    wb, ib_ = flatten(before, cfg)
    wa, ia = flatten(after, cfg)
    out = {"increased": [], "reset": [], "ignored": [], "state": []}
    for key, b in sorted(wb.items()):
        a = wa.get(key)
        if a is None:
            continue
        if a < b:
            out["reset"].append((key, b, a))
        elif a - b > limit:
            out["increased"].append((key, b, a))
    for key, b in sorted(ib_.items()):
        a = ia.get(key)
        if a is not None and a > b:
            out["ignored"].append((key, b, a))
    sb = parse_nvlink_status(((before or {}).get("smi") or {}).get("nvlink_status"))
    sa = parse_nvlink_status(((after or {}).get("smi") or {}).get("nvlink_status"))
    for gpu, links in sorted(sb.items()):
        for link, state in sorted(links.items()):
            now = sa.get(gpu, {}).get(link)
            if now is not None and link_active(state) and not link_active(now):
                out["state"].append("GPU{} link {}: {} -> {}".format(gpu, link, state, now))
    pb, pa = (before or {}).get("ib") or {}, (after or {}).get("ib") or {}
    for port, info in sorted(pb.items()):
        now = pa.get(port)
        if now and "ACTIVE" in info.get("state", "") and "ACTIVE" not in now.get("state", ""):
            out["state"].append("{}: {} -> {}".format(port, info.get("state"), now.get("state")))
    out["kernel"] = kernel_hits(after, cfg)
    return out


def delta_checks(d, names=None):
    """(checks, metrics, problems) for one host from delta()."""
    names = names or {}

    def label(key):
        port = key.split(":")[0]
        dev = port.split("/")[0]
        return "{} ({})".format(key, names[dev]) if dev in names else key

    checks, problems = {}, []
    inc = ["{} +{}".format(label(k), a - b) for k, b, a in d["increased"]]
    checks["link_errors"] = {"result": "fail" if inc else "pass",
                             "reason": "; ".join(inc)[:400] if inc else "no error counter went up"}
    if inc:
        problems.append("link error counters went up: " + "; ".join(inc)[:300])
    checks["link_state"] = {"result": "fail" if d["state"] else "pass",
                            "reason": "; ".join(d["state"])[:400] if d["state"] else "no link went down"}
    if d["state"]:
        problems.append("link went down: " + "; ".join(d["state"])[:300])
    kern = d.get("kernel") or []
    checks["kernel_log"] = {"result": "fail" if kern else "pass",
                            "reason": " | ".join(kern[-3:])[:400] if kern else "no GPU/NIC error in the kernel log"}
    if kern:
        problems.append("kernel log: {} new error line(s): {}".format(len(kern), kern[-1][:200]))
    metrics = {"link_error_increase": {"value": sum(a - b for _, b, a in d["increased"]), "unit": "",
                                       "lower_better": True, "category": "链路计数"}}
    return checks, metrics, problems


def state_checks(snap, cfg, host, nic=None):
    """L0 checks of one snapshot: GPUs, NVLink per GPU, P2P, IB ports. Returns a summary dict."""
    cfg = cfg or {}
    checks, metrics, problems = {}, {}, []
    names = rail_names(nic)

    def put(name, ok, reason, skip=False):
        checks[name] = {"result": "skip" if skip else ("pass" if ok else "fail"), "reason": reason}
        if not ok and not skip:
            problems.append("{}: {}".format(name, reason))

    smi = (snap or {}).get("smi") or {}
    rc = (snap or {}).get("rc") or {}
    gpus = parse_gpus(smi.get("gpus"))
    expect = getattr(host, "gpus", None) or 4
    if rc.get("gpus") not in (None, 0) and not gpus:
        put("gpu_count", False, "nvidia-smi failed: " + (smi.get("gpus") or "").strip()[:200])
    else:
        put("gpu_count", len(gpus) == expect, "{} GPUs (expected {})".format(len(gpus), expect))
    metrics["gpus"] = {"value": len(gpus), "unit": "", "category": "GPU"}

    status = parse_nvlink_status(smi.get("nvlink_status"))
    per_gpu = int(cfg.get("nvlink_per_gpu") or 18)
    if status:
        bad, speeds = [], []
        for gpu, links in sorted(status.items()):
            up = [l for l, s in links.items() if link_active(s)]
            down = sorted(set(links) - set(up))
            if len(up) < per_gpu:
                bad.append("GPU{} {}/{} up{}".format(gpu, len(up), per_gpu,
                                                     " (down: {})".format(",".join(map(str, down))) if down else ""))
            speeds += [(gpu, l, link_speed(links[l])) for l in up]
        known = [s for _, _, s in speeds if s]
        top = max(known) if known else None
        slow = ["GPU{} link {} {} GB/s".format(g, l, s) for g, l, s in speeds if s and top and s < top]
        put("nvlink_up", not bad, "; ".join(bad) or "{} links up on every GPU".format(per_gpu))
        put("nvlink_speed", not slow, "; ".join(slow)[:300] or "all links {} GB/s".format(top), skip=top is None)
        metrics["nvlink_up_min"] = {"value": min(len([1 for s in l.values() if link_active(s)])
                                                 for l in status.values()), "unit": "links", "category": "NVLink"}
    else:
        put("nvlink_up", False, "no nvidia-smi nvlink -s output: " + (smi.get("nvlink_status") or "").strip()[:200])

    if cfg.get("p2p", True):
        labels, rows = parse_p2p(smi.get("p2p"))
        if rows:
            bad = ["{}->{} {}".format(labels[i], labels[j], v) for i, r in enumerate(rows) for j, v in enumerate(r)
                   if i != j and v != "OK"]
            put("p2p", not bad, "; ".join(bad)[:300] or "all GPU pairs OK")
        else:
            put("p2p", True, "no nvidia-smi topo -p2p output", skip=True)

    ports = {p: i for p, i in ((snap or {}).get("ib") or {}).items() if i.get("link_layer", "").lower() == "infiniband"}
    want = cfg.get("ib_ports", 4)
    rate = cfg.get("ib_rate_gbps")
    if want:
        put("ib_ports", len(ports) == want, "{} IB ports (expected {})".format(len(ports), want))
    down = ["{}{} {}".format(p, " ({})".format(names[p.split("/")[0]]) if p.split("/")[0] in names else "",
                             i.get("state")) for p, i in sorted(ports.items()) if "ACTIVE" not in i.get("state", "")]
    put("ib_active", not down, "; ".join(down) or "all IB ports ACTIVE", skip=not ports)
    if rate and ports:
        slow = ["{} {}".format(p, i.get("rate")) for p, i in sorted(ports.items())
                if (ib_rate(i.get("rate")) or 0) < float(rate)]
        put("ib_rate", not slow, "; ".join(slow) or "all IB ports {} Gb/s".format(rate))
    if ports:
        metrics["ib_rate_min"] = {"value": min(ib_rate(i.get("rate")) or 0 for i in ports.values()), "unit": "Gb/s",
                                  "category": "IB"}

    watched, _ = flatten(snap, cfg.get("counters"))
    nonzero = {k: v for k, v in watched.items() if v}
    metrics["error_counters_nonzero"] = {"value": len(nonzero), "unit": "", "lower_better": True,
                                         "category": "链路计数"}
    limit = cfg.get("max_error_count")
    if limit is not None:
        over = ["{} = {}".format(k, v) for k, v in sorted(nonzero.items()) if v > int(limit)]
        put("error_counters", not over, "; ".join(over)[:400] or "no counter above {}".format(limit))

    kern = kernel_hits(snap, cfg.get("counters"))
    metrics["kernel_errors_since_boot"] = {"value": len(kern), "unit": "lines", "lower_better": True,
                                           "category": "内核日志"}
    klimit = cfg.get("max_kernel_errors")
    if klimit is not None:
        put("kernel_log", len(kern) <= int(klimit), " | ".join(kern[-3:])[:400] or "no GPU/NIC error since boot")
    elif kern:
        checks["kernel_log"] = {"result": "warn", "reason": "{} line(s) since boot: {}".format(
            len(kern), " | ".join(kern[-3:]))[:400]}

    ml = (snap or {}).get("mlxlink")
    if ml:
        cable_bad, rows, unavailable = [], [], []
        raw_max = eff_max = temp_max = None
        for port, doc in sorted(ml.items()):
            info = parse_mlxlink(doc)
            label = "{}{}".format(port, " ({})".format(names[port.split("/")[0]]) if port.split("/")[0] in names else "")
            if info.get("error"):
                unavailable.append("{}: {}".format(port, info["error"][:80]))
                continue
            eff = _num(info.get("effective_errors"))
            if eff:
                cable_bad.append("{} effective physical errors {}".format(label, int(eff)))
            for field, lim_key in (("effective_ber", "max_effective_ber"), ("raw_ber", "max_raw_ber"),
                                   ("temperature", "max_module_temp_c")):
                v = _num(info.get(field))
                lim = (cfg.get("mlxlink_limits") or {}).get(lim_key)
                if v is not None and lim is not None and v > float(lim):
                    cable_bad.append("{} {} {} > {}".format(label, field, info.get(field), lim))
            raw_max = max(filter(None, [raw_max, _num(info.get("raw_ber"))]), default=None)
            eff_max = max(filter(None, [eff_max, _num(info.get("effective_ber"))]), default=None)
            temp_max = max(filter(None, [temp_max, _num(info.get("temperature"))]), default=None)
        if unavailable and len(unavailable) == len(ml):
            put("ib_cable", True, "mlxlink not available: " + unavailable[0], skip=True)
        else:
            put("ib_cable", not cable_bad, "; ".join(cable_bad)[:400] or "no effective physical errors")
        for name, value, unit in (("ib_raw_ber_max", raw_max, ""), ("ib_effective_ber_max", eff_max, ""),
                                  ("ib_module_temp_max", temp_max, "C")):
            if value is not None:
                metrics[name] = {"value": value, "unit": unit, "lower_better": True, "category": "IB 线缆"}
    return {"status": "fail" if problems else "pass", "reason": "; ".join(problems)[:500],
            "metrics": metrics, "checks": checks}
