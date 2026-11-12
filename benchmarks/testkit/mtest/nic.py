"""S3: discover NICs on hosts, check them against network.expect, compare with the golden host."""

import os

from mtest import profiles
from mtest.executor import pmap
from mtest.nodecmd import NodeError, run_script
from mtest.ssh import Remote
from mtest.state import HostLocks, StateStore, load_nic, new_run_id, nic_path, now_iso, write_json
from mtest.util import dig

ITEM = "nic"


def _ids(value, default):
    """'15b3:1023' or ['15b3:1023', ...] -> {'1023', ...}"""
    vals = value if isinstance(value, list) else [value or default]
    return sorted({str(v).lower().split(":")[-1] for v in vals})


def summarize(raw, network):
    """Turn the node report into the stored record: rails, BF3 ports, LOM, mgmt/storage netdevs.

    mgmt/storage on a BF3 come from network.<role>.port here; on a plain NIC they are filled in by
    apply_plan from the plan PCI; mgmt on the LOM is the LOM interface.
    """
    pfs = (raw.get("bf3") or {}).get("pfs") or []
    pf = lambda i: pfs[i] if i < len(pfs) else {}

    def role_port(role, default):
        kind = profiles.device(network, role)
        if kind == "bf3":
            return pf(int(dig(network, role + ".port", default) or default))
        if kind == "lom":
            lom = raw.get("lom") or {}
            return {"netdev": lom.get("if", ""), "mac": lom.get("mac", "")}
        return {}
    mgmt, storage = role_port("mgmt", 0), role_port("storage", 1)
    rails = []
    for r in raw.get("rails", []):
        rails.append({"rail": r["rail"], "gpu": r.get("gpu"), "ibdev": r.get("ibdev", ""), "netdev": r.get("netdev", ""),
                      "pci": r["pci"], "numa": r.get("numa"), "link_layer": r.get("link_layer", ""),
                      "state": r.get("state", ""), "rate_gbps": r.get("rate_gbps"),
                      "link_type": r.get("link_type") or "", "link_type_next": r.get("link_type_next") or "",
                      "mac": r.get("mac", "")})
    bf3 = raw.get("bf3") or {}
    return {
        "mgmt_if": mgmt.get("netdev", ""), "mgmt_mac": mgmt.get("mac", ""),
        "storage_if": storage.get("netdev", ""), "storage_mac": storage.get("mac", ""),
        "ib_devs": [r["ibdev"] for r in rails if r["ibdev"]],
        "ipoib_ifs": [r["netdev"] for r in rails],
        "rails": rails,
        "bf3": {"mode": bf3.get("mode") or "unknown", "mode_next": bf3.get("mode_next") or "",
                "pf0": pf(0).get("netdev", ""), "pf1": pf(1).get("netdev", ""),
                "pf0_mac": pf(0).get("mac", ""), "pf1_mac": pf(1).get("mac", ""),
                "pci": ",".join(p["pci"] for p in pfs)},
        "lom": raw.get("lom") or {},
        "gpus": len(raw.get("gpus") or []),
        "mlxconfig": bool(raw.get("mlxconfig")),
        "raw": raw,
    }


def signature(rec):
    """What must be identical to the golden host: rail -> GPU, RDMA device, NUMA; BF3 port names."""
    return {"rails": [[r["rail"], r["gpu"], r["ibdev"], r["numa"]] for r in rec["rails"]],
            "bf3": [rec["bf3"]["pf0"], rec["bf3"]["pf1"]]}


def check(rec, network, golden=None):
    """{name: {"result": pass|fail|skip, "reason": ...}}."""
    exp = network.get("expect") or {}
    out = {}

    def put(name, ok, reason="", skip=False):
        out[name] = {"result": "skip" if skip else ("pass" if ok else "fail"), "reason": reason}

    if "gpus" in exp:
        put("gpus", rec["gpus"] == exp["gpus"], "{} GPUs (expected {})".format(rec["gpus"], exp["gpus"]))
    cx8 = exp.get("cx8") or {}
    n = len(rec["rails"])
    if "count" in cx8:
        put("cx8_count", n == cx8["count"], "{} compute NICs (expected {})".format(n, cx8["count"]))
    if cx8.get("link_layer"):
        bad = ["rail{} {}".format(r["rail"], r["link_layer"] or "?") for r in rec["rails"]
               if r["link_layer"] != cx8["link_layer"]]
        put("cx8_link_layer", not bad, ", ".join(bad) or cx8["link_layer"])
    bad = ["rail{} {}".format(r["rail"], r["state"] or "?") for r in rec["rails"] if r["state"] != "ACTIVE"]
    put("cx8_port_active", not bad, ", ".join(bad) or "all ACTIVE")
    if cx8.get("rate_gbps"):
        bad = ["rail{} {}".format(r["rail"], r["rate_gbps"]) for r in rec["rails"]
               if not r["rate_gbps"] or r["rate_gbps"] < cx8["rate_gbps"]]
        put("cx8_rate", not bad, ", ".join(bad) or ">= {} Gb/s".format(cx8["rate_gbps"]))
    if rec["gpus"]:
        put("gpu_rail_map", all(r["gpu"] is not None for r in rec["rails"]),
            "rail -> GPU " + ", ".join(str(r["gpu"]) for r in rec["rails"]))
    bf3 = exp.get("bf3") or {}
    pfs = len([p for p in (rec["bf3"]["pf0"], rec["bf3"]["pf1"]) if p])
    if "pf_count" in bf3:
        put("bf3_ports", pfs == bf3["pf_count"], "{} BF3 ports with a netdev (expected {})".format(pfs, bf3["pf_count"]))
    if bf3.get("mode"):
        mode = rec["bf3"]["mode"]
        if mode == "unknown":
            put("bf3_mode", True, "mlxconfig not available", skip=True)
        else:
            nic_mode = "DISABLED" in mode.upper() or mode.endswith("(1)")
            pending = rec["bf3"]["mode_next"] and rec["bf3"]["mode_next"] != mode
            want_nic = bf3["mode"] == "nic"
            put("bf3_mode", nic_mode == want_nic and not pending,
                "current {}, next boot {}".format(mode, rec["bf3"]["mode_next"] or mode)
                + ("; change pending a power cycle" if pending else ""))
    lom = rec["lom"]
    put("lom", bool(lom.get("if")), "{} {} gw {}".format(lom.get("if") or "?", lom.get("ip", ""), lom.get("gateway") or "?"))
    if golden is not None:
        a, b = signature(rec), signature(golden)
        diff = []
        if a["bf3"] != b["bf3"]:
            diff.append("BF3 ports {} vs {}".format(a["bf3"], b["bf3"]))
        for x, y in zip(a["rails"], b["rails"]):
            if x != y:
                diff.append("rail{}: gpu {} {} numa {} vs golden gpu {} {} numa {}".format(x[0], x[1], x[2], x[3],
                                                                                         y[1], y[2], y[3]))
        if len(a["rails"]) != len(b["rails"]):
            diff.append("{} rails vs golden {}".format(len(a["rails"]), len(b["rails"])))
        put("golden_match", not diff, "; ".join(diff) or "same as golden")
    return out


def apply_plan(rec, host, network=None):
    """Use the PCI addresses of the IP plan: pick the mgmt/storage ports by PCI and check that every
    planned PCI address exists. Returns the plan_pci check, or None when the plan has no PCI."""
    plan = host.plan
    raw = rec.get("raw") or {}
    pools = {"bf3": {p["pci"]: p for p in (raw.get("bf3") or {}).get("pfs") or []},
             "nic": {p["pci"]: p for p in raw.get("nics") or []}}
    rails = {r["pci"]: r for r in rec["rails"]}
    problems = []
    used = False
    for role in ("mgmt", "storage"):
        kind = profiles.device(network, role)
        if kind == "lom":
            continue
        pci = plan.get(role + "_pci")
        if not pci:
            if kind == "nic" and getattr(host, role, None) is not None:
                used = True
                problems.append("{}_pci is empty (network.{}.device is nic)".format(role, role))
            continue
        used = True
        pool = pools[kind]
        if pci in pool:
            rec[role + "_if"] = pool[pci].get("netdev", "")
            rec[role + "_mac"] = pool[pci].get("mac", "")
        elif kind == "bf3":
            problems.append("{}_pci {} not found (BF3: {})".format(role, pci, ", ".join(sorted(pool)) or "none"))
        else:
            problems.append("{}_pci {} not found".format(role, pci))
    for i, pci in enumerate(plan.get("ib_pci") or []):
        if not pci:
            continue
        used = True
        if pci not in rails:
            problems.append("ib_pci[{}] {} not found".format(i, pci))
        elif rails[pci]["rail"] != i:
            problems.append("ib_pci[{}] {} is rail {} by GPU distance".format(i, pci, rails[pci]["rail"]))
    if not used:
        return None
    return {"result": "fail" if problems else "pass", "reason": "; ".join(problems) or "all PCI addresses found"}


STRUCTURAL = ("cx8_count", "bf3_ports", "lom", "gpu_rail_map", "golden_match")


def discover(cluster, tests, hosts, out=print):
    """Run discovery on hosts; returns (run_id, {host: status})."""
    network = cluster.network or {}
    exp = network.get("expect") or {}
    remote = Remote(cluster.ssh)
    results = tests.results_dir
    run_id = new_run_id(results)
    run_dir = os.path.join(results, run_id)
    names = [h.name for h in hosts]
    locks = HostLocks(results)
    conflicts = locks.acquire(names, run_id, ITEM)
    if conflicts:
        for h, info in conflicts.items():
            out("error: {} is in use by run {}".format(h, info.get("run_id")))
        return run_id, {h: "blocked" for h in names}
    record = {"run_id": run_id, "pid": os.getpid(), "status": "running", "started_at": now_iso(),
              "items": {ITEM: {"status": "running", "hosts": names, "groups": {}}}}
    write_json(os.path.join(run_dir, "run.json"), record)
    args = {"cx8_ids": _ids(dig(exp, "cx8.pci_id"), "1023"), "bf3_ids": _ids(dig(exp, "bf3.pci_id"), "a2dc")}
    nic_roles = [r for r in ("mgmt", "storage") if profiles.device(network, r) == "nic"]

    def one(host):
        host_args = dict(args, nic_pcis=[host.plan[r + "_pci"] for r in nic_roles if host.plan.get(r + "_pci")])
        try:
            return host, summarize(run_script(remote, host, "nic_discover.py", host_args), network), None
        except NodeError as e:
            return host, None, str(e)

    try:
        found = pmap(one, hosts, int(cluster.ssh.get("parallel", 24)))
    finally:
        locks.release(names)
    started = record["started_at"]
    golden_host = next((h for h in cluster.hosts if h.golden), None)
    golden = None
    if golden_host is not None and network.get("expect", {}).get("compare_golden", True):
        golden = next((rec for h, rec, _ in found if h.name == golden_host.name and rec), None) \
            or load_nic(results, golden_host.name)
    statuses, entries = {}, {}
    for host, rec, err in found:
        if rec is None:
            status, reason, checks = "error", err, {}
        else:
            old = load_nic(results, host.name) or {}
            if not rec["lom"].get("gateway") and (old.get("lom") or {}).get("gateway"):
                rec["lom"]["gateway"] = old["lom"]["gateway"]
            ref = golden if golden is not None and host.name != golden_host.name else None
            plan_check = apply_plan(rec, host, network)
            checks = check(rec, network, ref)
            if plan_check:
                checks["plan_pci"] = plan_check
            failed = [k for k, v in checks.items() if v["result"] == "fail"]
            status = "fail" if failed else "pass"
            reason = "; ".join("{}: {}".format(k, checks[k]["reason"]) for k in failed)
            rec.update({"host": host.name, "run_id": run_id, "discovered_at": now_iso(), "checks": checks,
                        "status": status})
            write_json(nic_path(results, host.name), rec)
        metrics = {}
        if rec:
            metrics = {"gpus": {"value": rec["gpus"], "unit": ""}, "cx8": {"value": len(rec["rails"]), "unit": ""},
                       "mgmt_if": {"value": rec["mgmt_if"]}, "bf3_mode": {"value": rec["bf3"]["mode"]}}
            for r in rec["rails"]:
                metrics["rail{}".format(r["rail"])] = {"value": "{} gpu{} {} {}".format(
                    r["ibdev"], r["gpu"], r["state"] or "?", r["rate_gbps"] or "?")}
        summary = {"item": ITEM, "hostname": host.name, "run_id": run_id, "status": status, "reason": reason,
                   "group": host.name, "rank": 0, "group_result": False, "started_at": started,
                   "finished_at": now_iso(), "metrics": metrics, "checks": checks}
        write_json(os.path.join(run_dir, host.name, ITEM, "summary.json"), summary)
        entries[host.name] = {ITEM: {"status": status, "reason": reason, "run_id": run_id, "group": host.name,
                                     "rank": 0, "group_result": False, "finished_at": summary["finished_at"]}}
        statuses[host.name] = status
        mark = "ok  " if status == "pass" else status.upper()
        out("{} {:<20} {}".format(mark, host.name, reason if reason else _line(rec)))
    StateStore(results).update(entries)
    final = "pass" if all(s == "pass" for s in statuses.values()) else "fail"
    record["items"][ITEM]["status"] = final
    record.update({"status": final, "finished_at": now_iso()})
    write_json(os.path.join(run_dir, "run.json"), record)
    return run_id, statuses


def _line(rec):
    if not rec:
        return ""
    return "mgmt {} storage {} rails {} bf3 {}".format(rec["mgmt_if"] or "?", rec["storage_if"] or "?",
                                                       ",".join(rec["ib_devs"]) or "-", rec["bf3"]["mode"])


def show(cluster, tests, name, out=print):
    rec = load_nic(tests.results_dir, name)
    if not rec:
        out("{}: no NIC data; run mtest nic discover".format(name))
        return 1
    out("{}  ({}, run {}, {})".format(name, rec.get("status"), rec.get("run_id"), rec.get("discovered_at")))
    out("  LOM      {} {} mac {} gateway {}".format(rec["lom"].get("if"), rec["lom"].get("ip"), rec["lom"].get("mac"),
                                                     rec["lom"].get("gateway")))
    out("  BF3      mode {} (next {})  pf0 {} {}  pf1 {} {}".format(
        rec["bf3"]["mode"], rec["bf3"]["mode_next"] or "-", rec["bf3"]["pf0"], rec["bf3"]["pf0_mac"],
        rec["bf3"]["pf1"], rec["bf3"]["pf1_mac"]))
    out("  rail gpu ibdev     netdev        pci            numa state   rate  link_type")
    for r in rec["rails"]:
        out("  {:<4} {:<3} {:<9} {:<13} {:<14} {:<4} {:<7} {:<5} {}".format(
            r["rail"], "-" if r["gpu"] is None else r["gpu"], r["ibdev"], r["netdev"], r["pci"], r["numa"],
            r["state"] or "?", r["rate_gbps"] or "?", r["link_type"] or "-"))
    for k, v in (rec.get("checks") or {}).items():
        out("  {:<5} {:<16} {}".format(v["result"], k, v["reason"]))
    return 0


def structural_problems(rec):
    """Failed checks that make network configuration unsafe."""
    return ["{}: {}".format(k, v["reason"]) for k, v in (rec.get("checks") or {}).items()
            if k in STRUCTURAL and v["result"] == "fail"]

