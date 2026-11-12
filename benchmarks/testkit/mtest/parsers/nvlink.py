"""Results of the S7b nvlink items (node_scripts/nvlink_run.py on the rank 0 host).

raw/done.json            steps run, with nvlink_run.py's verdict
raw/pairs.txt            dcgmi mndiag text output of mnnvbandwidth gpuPairs=all (pair counts are text only)
raw/<step>.txt           dcgmi mndiag --json output of the other steps
raw/<step>-logs/...      workload logs written during the step: nvbandwidth / nvloom_cli output

nvbandwidth prints "rank N (host), device D" lines and, per testcase, "Running <key>." followed by a
description, a header of column labels and one row per GPU (rank). nvloom_cli prints
"Process NNN (host): device D: ..." lines and, per testcase, "Running <name>", a header, rows, then
"Min/Avg/Med/Max/Total <name> <value> <unit>". Both are turned into per-GPU outliers against the
median of the rack, and into per-host verdicts.
"""

import json
import os
import re

from mtest.outlier import deviation, median, outliers, threshold

DEFAULT_PCT = 10


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _float(word):
    word = word.split(";")[0]
    try:
        return float(word)
    except ValueError:
        return None


# ---------- dcgmi mndiag ----------

def parse_pairs_text(text):
    """Pair counts, error lines and log files of an all-pairs run (text output)."""
    def count(label):
        m = re.search(r"Pairs {}\D*(\d+)".format(label), text or "")
        return int(m.group(1)) if m else None
    errors, logs = [], []
    section = None
    for line in (text or "").splitlines():
        s = line.strip().strip("|").strip()
        low = s.lower()
        if low.startswith("error summary"):
            section = "errors"
            continue
        if low.startswith("log files"):
            section = "logs"
            logs += re.findall(r"(/[\w./-]+)", s)
            continue
        if not s or set(s) <= set("+-=|"):
            continue
        if section == "errors" and not low.startswith(("metadata", "host details")):
            errors.append(s)
        elif section == "logs":
            logs += re.findall(r"(/[\w./-]+)", s)
        if low.startswith(("metadata", "host details")):
            section = None
    return {"planned": count("Planned"), "passed": count("Passed"), "failed": count("Failed"),
            "errors": errors, "logs": logs, "ok_sentence": "ran successfully" in (text or "")}


def parse_mndiag_json(text):
    """{"status", "error", "tests": [(name, status)], "failed": [(hostname, entity)], "messages": [...]}"""
    start = (text or "").find("{")
    try:
        doc = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return {"status": "error", "error": "no JSON output from dcgmi mndiag", "tests": [], "failed": [],
                "messages": []}
    hosts = {h.get("host_id"): h.get("hostname", "") for h in doc.get("hosts") or [] if isinstance(h, dict)}
    tests, failed, messages = [], [], []
    for cat in doc.get("test_categories") or []:
        for t in cat.get("tests") or []:
            status = str(t.get("status") or (t.get("test_summary") or {}).get("status") or "")
            tests.append((t.get("name", "?"), status))
            for r in t.get("results") or []:
                ent = "{}{}".format("GPU" if str(r.get("entity_group", "GPU")).upper() == "GPU"
                                    else r.get("entity_group", ""), r.get("entity_id", ""))
                host = hosts.get(r.get("host_id"), str(r.get("host_id", "")))
                if str(r.get("status", "")).lower() == "fail":
                    failed.append((host, ent))
                for w in r.get("warnings") or []:
                    messages.append("{} {}: {}".format(host, ent, w.get("warning", "") if isinstance(w, dict) else w))
            if t.get("mpi_error"):
                messages.append("MPI: {}".format(t["mpi_error"]))
    for e in doc.get("global_errors") or []:
        if isinstance(e, dict):
            messages.append("{}{}".format(hosts.get(e.get("host_id"), ""), (": " if e.get("host_id") is not None else "")
                                          + str(e.get("warning", ""))))
    statuses = [s.lower() for _, s in tests]
    if doc.get("runtime_error"):
        status = "error"
    elif statuses and all(s == "pass" for s in statuses):
        status = "pass"
    elif statuses and all(s == "skip" for s in statuses):
        status = "skip"
    else:
        status = "fail" if statuses else "error"
    return {"status": status, "error": doc.get("runtime_error"), "tests": tests, "failed": failed,
            "messages": [m for m in messages if m.strip()]}


# ---------- workload output ----------

def parse_nvbandwidth(text):
    """{"ranks": {rank: (host, device)}, "tests": {key: {"desc", "cols", "rows": [(label, [values])]}}}"""
    ranks, tests = {}, {}
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*rank (\d+) \(([^)]*)\), device (\d+)", line)
        if m:
            ranks[int(m.group(1))] = (m.group(2), int(m.group(3)))
            i += 1
            continue
        m = re.match(r"^Running (\S+?)\.?\s*$", line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines) or ("bandwidth" not in lines[i] and "latency" not in lines[i]):
            continue
        desc = lines[i].strip()
        i += 1
        if i >= len(lines):
            break
        cols = lines[i].split()
        rows = []
        i += 1
        while i < len(lines) and lines[i].strip():
            parts = lines[i].split()
            if len(parts) == len(cols) + 1:
                rows.append((parts[0], [_float(w) for w in parts[1:]]))
            i += 1
        if rows:
            tests[key] = {"desc": desc, "cols": cols, "rows": rows,
                          "lower_better": "latency" in key or "latency" in desc}
    return {"ranks": ranks, "tests": tests}


def parse_nvloom(text):
    """{"procs": {index: (host, device)}, "tests": {name: {"cols", "rows", "unit", "stats", "waived"}}}"""
    procs, tests = {}, {}
    lines = [l for l in (text or "").splitlines() if not l.startswith("sleeping for")]
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^Process (\d+) \(([^)]*)\): device (\d+)", line)
        if m:
            procs[int(m.group(1))] = (m.group(2), int(m.group(3)))
            i += 1
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] != "Running":
            i += 1
            continue
        name = parts[1]
        i += 1
        if i < len(lines) and "WAIVED" in lines[i]:
            tests[name] = {"waived": True, "cols": [], "rows": [], "unit": "", "stats": {}}
            i += 1
            continue
        if i >= len(lines):
            break
        cols = [w for w in lines[i].split() if w != "|"]
        rows = []
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith("Min "):
            words = [w for w in lines[i].split() if w != "|"]
            if words:
                rows.append((words[0], [_float(w) if w != "N/A" else None for w in words[1:]]))
            i += 1
        stats, unit = {}, "GB/s"
        while i < len(lines) and not lines[i].startswith("Done "):
            m = re.match(r"^(Min|Avg|Med|Max|Total) (\S+) (\S+)(?: (\S+))?", lines[i])
            if m and m.group(2) == name:
                stats[m.group(1).lower()] = _float(m.group(3))
                unit = m.group(4) or unit
            elif lines[i].startswith("Running "):
                break
            i += 1
        tests[name] = {"waived": False, "cols": cols, "rows": rows, "unit": unit, "stats": stats,
                       "lower_better": unit in ("ns", "us") or "latency" in name}
    return {"procs": procs, "tests": tests}


def _workload_texts(raw, step):
    """Texts that may hold workload output for one step: its stdout and every file in <step>-logs."""
    out = [_read(os.path.join(raw, step + ".txt"))]
    base = os.path.join(raw, step + "-logs")
    for root, _, files in os.walk(base):
        for f in sorted(files):
            out.append(_read(os.path.join(root, f)))
    return out


def _merge(parsed_list, key_maps, key_tests):
    """Merge several parse results; for each testcase keep the one with the most rows."""
    maps, tests = {}, {}
    for p in parsed_list:
        maps.update(p[key_maps])
        for k, t in p[key_tests].items():
            if k not in tests or len(t.get("rows") or []) > len(tests[k].get("rows") or []):
                tests[k] = t
    return maps, tests


# ---------- analysis ----------

def _gpu_label(label, ids, names):
    """Row/column label -> "host:GPUn" via the rank/process table; other labels stay as they are."""
    if label.isdigit() and int(label) in ids:
        host, dev = ids[int(label)]
        return "{}:GPU{}".format(names.get(host, names.get(host.split(".")[0], host)), dev)
    return label


def analyse_matrix(name, test, ids, names, pct, baseline=None):
    """Outliers of one matrix. Rows are source GPUs; columns are peer GPUs when labelled like the rows."""
    lb = bool(test.get("lower_better"))
    rows = test.get("rows") or []
    cols = test.get("cols") or []
    row_labels = [r for r, _ in rows]
    square = sorted(cols) == sorted(row_labels) and len(cols) == len(rows)
    pairs = {}
    for r, vals in rows:
        for c, v in zip(cols, vals):
            if v is not None and v > 0:
                pairs[(r, c)] = v
    if not pairs:
        return None
    med = median(pairs.values())
    gpu_vals = {}
    for r, vals in rows:
        gpu_vals[("src", r)] = median([v for v in vals if v])
    if square:
        for ci, c in enumerate(cols):
            gpu_vals[("dst", c)] = median([vals[ci] for _, vals in rows if ci < len(vals) and vals[ci]])
    bad_gpus = outliers(gpu_vals, pct, lb)
    bad_pairs = outliers(pairs, pct, lb)
    below = []
    if baseline is not None:
        below = [(k, v) for k, v in pairs.items() if (v > baseline if lb else v < baseline)]
    label = lambda x: _gpu_label(x, ids, names)
    return {
        "name": name, "unit": test.get("unit") or ("ns" if lb else "GB/s"), "lower_better": lb,
        "count": len(pairs), "min": min(pairs.values()), "median": med, "max": max(pairs.values()),
        "limit": threshold(med, pct, lb),
        "gpus": {label(k[1]): v for k, v in gpu_vals.items() if k[0] == "src" and v is not None},
        "bad_gpus": [(k[0], label(k[1]), v, m) for k, (v, m) in sorted(bad_gpus.items())],
        "bad_pairs": [(label(k[0]), label(k[1]), v, m) for k, (v, m) in sorted(bad_pairs.items())],
        "below_baseline": [(label(k[0]), label(k[1]), v) for k, v in sorted(below)],
        "baseline": baseline,
    }


def _host_of(gpu_label):
    return gpu_label.split(":GPU")[0] if ":GPU" in gpu_label else None


def parse_group(local_dir, item_id, cfg, names, hosts, total_gpus):
    """(per_host, extra) for one nvlink group. names: OS hostname / address -> mtest name."""
    raw = os.path.join(local_dir, "raw")
    pct = float(cfg.get("outlier_pct", DEFAULT_PCT))
    action = cfg.get("outlier_action", "fail")
    baselines = cfg.get("baseline") or {}
    done = {}
    try:
        with open(os.path.join(local_dir, "done.json"), encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        pass
    steps = done.get("steps") or []
    if not steps:
        return {}, {"status": "error", "reason": "no done.json from nvlink_run.py (see the rank 0 host)"}
    host_problems = {h: [] for h in hosts}
    host_metrics = {h: {} for h in hosts}
    problems = []
    step_rows, err_rows, gpu_rows, pair_rows, case_rows = [], [], [], [], []
    summary_bits = []

    def blame(text, why):
        hit = sorted({n for key, n in names.items() if key and re.search(r"(^|[^\w.-]){}($|[^\w.-])".format(
            re.escape(key)), text)})
        for h in hit:
            if h in host_problems:
                host_problems[h].append(why)
        return hit

    for st in steps:
        name = st["name"]
        detail = st.get("reason", "")
        if st.get("verdict") == "skip":
            step_rows.append([name, "skip", detail, st.get("seconds", 0)])
            continue
        text = _read(os.path.join(raw, name + ".txt"))
        if name == "pairs":
            p = parse_pairs_text(text)
            detail = "Planned {} / Passed {} / Failed {}".format(p["planned"], p["passed"], p["failed"])
            summary_bits.append("GPU 对 {}/{}/{}".format(p["planned"], p["passed"], p["failed"]))
            expect = total_gpus * (total_gpus - 1) // 2
            if p["planned"] is not None and p["planned"] != expect:
                problems.append("pairs planned {} != {} ({} GPUs)".format(p["planned"], expect, total_gpus))
            for line in p["errors"]:
                hit = blame(line, "GPU pair failed")
                err_rows.append([name, ", ".join(hit) or "-", line[:300]])
            for h in hosts:
                host_metrics[h]["pairs_failed"] = {
                    "value": sum(1 for why in host_problems[h] if why == "GPU pair failed"), "unit": "",
                    "lower_better": True, "category": "GPU 对", "name": "失败的 GPU 对（错误中出现次数）"}
        else:
            j = parse_mndiag_json(text)
            detail = "{}{}".format(", ".join("{} {}".format(n_, s) for n_, s in j["tests"]) or j["status"],
                                   "; " + j["error"] if j["error"] else "")
            for host, ent in j["failed"]:
                h = names.get(host, names.get(host.split(".")[0], host))
                if h in host_problems:
                    host_problems[h].append("{} {} failed in {}".format(ent, name, "dcgmi"))
                err_rows.append([name, h, "{} Fail".format(ent)])
            for msg in j["messages"]:
                hit = blame(msg, "{}: error".format(name))
                err_rows.append([name, ", ".join(hit) or "-", msg[:300]])
        if st.get("verdict") != "pass":
            problems.append("{}: {} ({})".format(name, st.get("verdict"), (st.get("reason") or "")[:200]))
        step_rows.append([name, st.get("verdict", "?"), detail[:300], st.get("seconds", 0)])

        if name == "pairs":
            continue
        texts = _workload_texts(raw, name)
        if name == "bandwidth":
            ids, tests = _merge([parse_nvbandwidth(t) for t in texts], "ranks", "tests")
        else:
            ids, tests = _merge([parse_nvloom(t) for t in texts], "procs", "tests")
        if not tests and st.get("verdict") == "pass":
            problems.append("{}: no measurements found in the workload logs".format(name))
        for case, test in sorted(tests.items()):
            if test.get("waived"):
                case_rows.append([name, case, "WAIVED", "", "", "", "", ""])
                continue
            a = analyse_matrix(case, test, ids, names, pct, baselines.get(case))
            if a is None:
                continue
            case_rows.append([name, case, a["unit"], a["count"], round(a["min"], 2), round(a["median"], 2),
                              round(a["max"], 2), len(a["bad_gpus"])])
            for side, gpu, v, m in a["bad_gpus"]:
                gpu_rows.append([case, gpu, "源" if side == "src" else "目的", round(v, 2), round(m, 2),
                                 deviation(v, m)])
                h = _host_of(gpu)
                if h in host_problems and action == "fail":
                    host_problems[h].append("{} {} {:.1f} vs median {:.1f}".format(case, gpu, v, m))
            for s_, d_, v, m in a["bad_pairs"][:200]:
                pair_rows.append([case, s_, d_, round(v, 2), round(m, 2), deviation(v, m)])
            for s_, d_, v in a["below_baseline"]:
                for gpu in (s_, d_):
                    h = _host_of(gpu)
                    if h in host_problems:
                        host_problems[h].append("{} {}->{} {:.1f} below baseline {}".format(case, s_, d_, v,
                                                                                         a["baseline"]))
            if a["below_baseline"]:
                problems.append("{}: {} pairs below baseline {}".format(case, len(a["below_baseline"]),
                                                                       a["baseline"]))
            if a["bad_gpus"] and action == "fail":
                problems.append("{}: {} GPU outliers (>{}% from median)".format(case, len(a["bad_gpus"]), pct))
            for gpu, v in a["gpus"].items():
                h = _host_of(gpu)
                if h in host_metrics:
                    key = "{}/{}".format(name, case)
                    cur = host_metrics[h].get(key)
                    if cur is None or (v > cur["value"] if a["lower_better"] else v < cur["value"]):
                        host_metrics[h][key] = {"value": round(v, 2), "unit": a["unit"],
                                                "lower_better": a["lower_better"], "category": name,
                                                "name": "{} 最差 GPU 中位数".format(case)}
        if tests:
            summary_bits.append("{} {} 个用例".format(name, len(tests)))

    per_host = {}
    for h in hosts:
        bad = host_problems[h]
        checks = {"nvlink": {"result": "fail" if bad else "pass",
                             "reason": "; ".join(bad)[:400] if bad else "no failed pair or outlier GPU"}}
        entry = {"metrics": host_metrics[h], "checks": checks}
        if bad:
            entry.update({"status": "fail", "reason": "; ".join(bad)[:400], "own": True})
        per_host[h] = entry
    tables = [{"title": "步骤", "head": ["步骤", "结果", "详情", "秒"], "rows": step_rows}]
    if case_rows:
        tables.append({"title": "测试用例汇总", "head": ["步骤", "用例", "单位", "值个数", "最小", "中位数", "最大",
                                                   "离群 GPU"], "rows": case_rows})
    if gpu_rows:
        tables.append({"title": "离群 GPU（与 rack 中位数相差超过 {}%）".format(pct),
                       "head": ["用例", "GPU", "方向", "GPU 中位数", "rack 中位数", "偏差 %"], "rows": gpu_rows})
    if pair_rows:
        tables.append({"title": "离群 GPU 对（前 200 个）", "head": ["用例", "源", "目的", "值", "中位数", "偏差 %"],
                       "rows": pair_rows})
    if err_rows:
        tables.append({"title": "错误", "head": ["步骤", "主机", "内容"], "rows": err_rows[:200]})
    extra = {"tables": tables, "summary": "；".join(summary_bits) or "{} 节点".format(len(hosts))}
    if problems:
        extra.update({"status": "fail", "reason": "; ".join(problems)[:500]})
    return per_host, extra
