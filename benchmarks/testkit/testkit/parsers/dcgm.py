"""Turn the raw output of node_scripts/dcgm_run.sh into summary.json metrics and checks.

Inputs (under <host>/<item>/raw/): diag.json (dcgmi diag -j, DCGM 4.x schema), diag.rc,
health.json, nvidia-smi-q.txt, nvlink.txt, gpus.csv.
"""

import json
import os
import re

FAIL_STATUS = ("fail",)
SKIP_STATUS = ("skip", "not run", "notrun", "")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _json(text):
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def parse_diag(doc):
    """{"error": str|None, "tests": [{"name", "category", "status", "gpus_failed", "warnings"}], "version"}"""
    if not isinstance(doc, dict):
        return {"error": "no JSON output from dcgmi diag", "tests": [], "version": None}
    body = doc.get("DCGM Diagnostic") or doc.get("DCGM GPU Diagnostic") or {}
    tests = []
    for cat in body.get("test_categories") or []:
        for t in cat.get("tests") or []:
            results = t.get("results") or []
            status = str((t.get("test_summary") or {}).get("status") or "").strip()
            warnings, failed = [], []
            for r in results:
                rs = str(r.get("status", "")).strip().lower()
                group = str(r.get("entity_group", "GPU"))
                ent = "{}{}".format("gpu" if group.upper() == "GPU" else group.lower(),
                                    r.get("entity_id", r.get("gpu_id", "")))
                if rs in FAIL_STATUS:
                    failed.append(ent)
                for w in r.get("warnings") or []:
                    msg = w.get("warning") if isinstance(w, dict) else str(w)
                    if msg:
                        warnings.append("{}: {}".format(ent, msg.strip()))
            if not status:
                statuses = {str(r.get("status", "")).strip().lower() for r in results}
                status = "Fail" if statuses & set(FAIL_STATUS) else ("Pass" if "pass" in statuses else "Skip")
            tests.append({"name": t.get("name", "?"), "category": cat.get("category", ""), "status": status,
                          "gpus_failed": failed, "warnings": warnings})
    error = body.get("runtime_error")
    if not tests and not error:
        error = "dcgmi diag reported no tests"
    return {"error": error.strip() if error else None, "tests": tests,
            "version": body.get("version") or (doc.get("metadata") or {}).get("version")}


def _gpu_sections(text):
    """nvidia-smi -q split per GPU: {bus_id: section text}."""
    out = {}
    parts = re.split(r"^GPU ([0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.\d)\s*$", text or "", flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1]
    return out


def _block(text, title):
    """Lines of the indented block under the line 'title' (until a line at the same or lower indent)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == title:
            indent = len(line) - len(line.lstrip())
            out = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                out.append(nxt)
            return out
    return []


def _kv(lines):
    """key: value pairs of the direct children of a block (nested blocks are ignored)."""
    body = [l for l in lines if l.strip()]
    if not body:
        return {}
    top = min(len(l) - len(l.lstrip()) for l in body)
    out = {}
    for line in body:
        if len(line) - len(line.lstrip()) == top and ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _num(v):
    return int(v) if str(v).isdigit() else 0


def parse_smi(text):
    """Per GPU from nvidia-smi -q: uncorrectable ECC (volatile), remapped rows, fabric state."""
    gpus = []
    for bus, sec in _gpu_sections(text).items():
        vol = _block("\n".join(_block(sec, "ECC Errors")), "Volatile")
        unc = sum(int(v) for k, v in _kv(vol).items() if "uncorrectable" in k.lower() and v.isdigit())
        rr = _kv(_block(sec, "Remapped Rows"))
        fabric = _kv(_block(sec, "Fabric"))
        gpus.append({"bus": bus, "ecc_uncorrectable": unc,
                     "remapped_uncorrectable": _num(rr.get("Uncorrectable Error", 0)),
                     "remap_pending": rr.get("Pending", "No"),
                     "remap_failure": rr.get("Remapping Failure Occurred", "No"),
                     "fabric_state": fabric.get("State", ""), "fabric_status": fabric.get("Status", "")})
    return gpus


def parse_nvlink(text):
    """{gpu_index: (links_up, links_total)} from 'nvidia-smi nvlink -s'."""
    out = {}
    gpu = None
    for line in (text or "").splitlines():
        m = re.match(r"^GPU (\d+):", line)
        if m:
            gpu = int(m.group(1))
            out[gpu] = [0, 0]
            continue
        m = re.match(r"^\s+Link \d+:\s*(.*)$", line)
        if m and gpu is not None:
            out[gpu][1] += 1
            if "inactive" not in m.group(1).lower() and re.search(r"\d", m.group(1)):
                out[gpu][0] += 1
    return {k: tuple(v) for k, v in out.items()}


def parse_health(doc):
    if not isinstance(doc, dict):
        return None
    return ((doc.get("body") or {}).get("Overall Health") or {}).get("value")


def parse(local_dir, cfg, host):
    """Summary fields for one host: {"status", "reason", "metrics", "checks"}."""
    raw = os.path.join(local_dir, "raw")
    checks, metrics, problems = {}, {}, []

    def put(name, ok, reason="", skip=False):
        checks[name] = {"result": "skip" if skip else ("pass" if ok else "fail"), "reason": reason}
        if not ok and not skip:
            problems.append("{}: {}".format(name, reason))

    diag = parse_diag(_json(_read(os.path.join(raw, "diag.json"))))
    if diag["error"]:
        put("diag", False, " / ".join(l.strip() for l in diag["error"].splitlines() if l.strip())[:400])
    for t in diag["tests"]:
        s = t["status"].lower()
        detail = ", ".join(t["gpus_failed"])
        if t["warnings"]:
            detail = (detail + "; " if detail else "") + "; ".join(t["warnings"][:3])
        name = "diag:" + t["name"]
        if s in FAIL_STATUS:
            put(name, False, detail or "Fail")
        elif s in SKIP_STATUS:
            put(name, True, t["status"] or "not run", skip=True)
        else:
            put(name, True, t["status"] + ("; " + detail if detail else ""))
    metrics["diag_level"] = {"value": cfg.get("level", 3), "unit": "", "category": "dcgmi diag"}
    metrics["diag_tests"] = {"value": len(diag["tests"]), "unit": "", "category": "dcgmi diag"}

    smi = parse_smi(_read(os.path.join(raw, "nvidia-smi-q.txt")))
    expect = getattr(host, "gpus", None) or 4
    put("gpu_count", len(smi) == expect, "{} GPUs (expected {})".format(len(smi), expect))
    metrics["gpus"] = {"value": len(smi), "unit": "", "category": "nvidia-smi"}
    wanted = cfg.get("checks") or ["ecc", "remapped_rows", "nvlink", "fabric_state"]
    if smi:
        unc = sum(g["ecc_uncorrectable"] for g in smi)
        metrics["ecc_uncorrectable"] = {"value": unc, "unit": "", "lower_better": True, "category": "nvidia-smi"}
        if "ecc" in wanted:
            bad = ["{} {}".format(g["bus"], g["ecc_uncorrectable"]) for g in smi if g["ecc_uncorrectable"]]
            put("ecc", not bad, ("uncorrectable ECC: " + ", ".join(bad)) if bad else "no uncorrectable ECC")
        rr = sum(g["remapped_uncorrectable"] for g in smi)
        metrics["remapped_rows_uncorrectable"] = {"value": rr, "unit": "", "lower_better": True,
                                                  "category": "nvidia-smi"}
        if "remapped_rows" in wanted:
            bad = [g["bus"] for g in smi if g["remapped_uncorrectable"] or g["remap_pending"].lower() == "yes"
                   or g["remap_failure"].lower() == "yes"]
            put("remapped_rows", not bad,
                ("uncorrectable, pending or failed row remapping on " + ", ".join(bad)) if bad else "ok")
        if "fabric_state" in wanted:
            known = any(g["fabric_state"] for g in smi)
            bad = ["{} {}/{}".format(g["bus"], g["fabric_state"] or "?", g["fabric_status"] or "?") for g in smi
                   if g["fabric_state"] != "Completed" or (g["fabric_status"] and g["fabric_status"] != "Success")]
            put("fabric_state", not bad, ", ".join(bad) or "Completed", skip=not known)
    if "nvlink" in wanted:
        links = parse_nvlink(_read(os.path.join(raw, "nvlink.txt")))
        if links:
            down = ["GPU{} {}/{}".format(g, up, total) for g, (up, total) in sorted(links.items()) if up < total]
            metrics["nvlink_up_min"] = {"value": min(up for up, _ in links.values()), "unit": "links",
                                        "category": "nvidia-smi"}
            put("nvlink", not down, ("links up: " + ", ".join(down)) if down else "all links up")
        else:
            put("nvlink", True, "no NVLink output", skip=True)
    if cfg.get("health", True):
        h = parse_health(_json(_read(os.path.join(raw, "health.json"))))
        if h is None:
            put("health", True, "no health output", skip=True)
        else:
            put("health", h == "Healthy", "Overall Health: " + h)
    if (cfg.get("gpu_burn") or {}).get("enabled"):
        put("gpu_burn", True, "gpu_burn is not run by the dcgm item yet", skip=True)
    return {"status": "fail" if problems else "pass", "reason": "; ".join(problems)[:500],
            "metrics": metrics, "checks": checks}
