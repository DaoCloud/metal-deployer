"""Synthetic results covering every report branch: node items with metrics and checks,
a failing host with a log, group items with tables / charts / matrix, versions, NIC data."""

import os

from testkit.config import load_both
from testkit.state import StateStore, nic_path, write_json
from tests.helpers import Workspace

RUN = "20261112-093000"


def _summary(ws_results, host, item, status, metrics=None, checks=None, rank=0, group=None, extra=None):
    data = {"item": item, "hostname": host, "run_id": RUN, "status": status, "rank": rank, "group": group or host,
            "started_at": "2026-11-12T09:30:00Z", "finished_at": "2026-11-12T09:42:00Z",
            "image": "img:1", "image_digest": "sha256:abc", "metrics": metrics or {}, "checks": checks or {}}
    data.update(extra or {})
    write_json(os.path.join(ws_results, RUN, host, item, "summary.json"), data)


def make_results(racks=2, trays=4):
    ws = Workspace(racks, trays)
    ws.edit("cluster.yaml", "    # skip: [moe]", "    skip: [moe]")
    cluster, tests, _ = load_both(ws.cluster, ws.tests)
    res = tests.results_dir
    store = StateStore(res)
    names = [h.name for h in cluster.hosts]
    entries = {}
    for i, name in enumerate(names):
        status = "fail" if i == 3 else "pass"
        metrics = {"gemm_fp8": {"value": 4500.0 - (900 if i == 5 else 0) + i, "unit": "TFLOPS",
                                "category": "GPU-COMPUTE"},
                   "latency": {"value": 2.0 + (0.5 if i == 6 else 0), "unit": "us", "category": "RDMA",
                               "lower_better": True},
                   "driver": {"value": "580.95"}}
        checks = {"ecc": {"result": "fail" if i == 3 else "pass"}, "nvlink": {"result": "pass"}}
        _summary(res, name, "dcgm", status, metrics, checks)
        env = {"driver": "580.95.05", "cuda": "13.0", "cx8_fw": "40.44.1036" if i == 2 else "40.46.1018"}
        write_json(os.path.join(res, RUN, name, "dcgm", "env.json"), env)
        if i == 3:
            with open(os.path.join(res, RUN, name, "dcgm", "container.log"), "w") as f:
                f.write("Memory ..... Fail - GPU2\n</script><b>x</b>\n")
        entries.setdefault(name, {})["dcgm"] = {"status": status, "run_id": RUN, "group": name, "rank": 0,
                                                "group_result": False, "reason": "exit 1" if i == 3 else ""}
        write_json(nic_path(res, name), {"mgmt_if": "enP2p1s0f0np0", "ib_devs": ["mlx5_0", "mlx5_1"],
                                         "rails": [{"gpu": 0, "ibdev": "mlx5_0"}, {"gpu": 1, "ibdev": "mlx5_1"}],
                                         "bf3": {"mode": "NIC"}})
    for rack in cluster.racks:
        members = [h.name for h in cluster.rack_hosts(rack) if "moe" not in h.skip]
        status = "fail" if rack == "R1" else "pass"
        for rank, name in enumerate(members):
            _summary(res, name, "moe.rack", status, {"dispatch": {"value": 400.0 + rank, "unit": "GB/s"}},
                     rank=rank, group=rack)
            entries.setdefault(name, {})["moe.rack"] = {"status": status, "run_id": RUN, "group": rack, "rank": rank,
                                                        "group_result": True, "reason": "launcher exit 1" if
                                                        status == "fail" else ""}
        group = {"item": "moe.rack", "group": rack, "mode": "per-node", "status": status, "run_id": RUN,
                 "reason": "launcher exit 1" if status == "fail" else "", "hosts": members, "seconds": 120,
                 "first_failed": members[0] if status == "fail" else None,
                 "tables": [{"title": "pairs", "head": ["a", "b", "bw"], "num_cols": [2],
                             "rows": [[members[0], members[1], 97.5], [members[1], members[2], 80.0]]}],
                 "charts": [{"title": "busbw", "unit": "GB/s", "x": [1048576, 2097152, 4194304],
                             "series": [{"name": "allreduce", "y": [100, 200, 300]}]}],
                 "matrix": {"labels": ["t01", "t02"], "cells": [["C", "C"], ["C", "-"]]}}
        write_json(os.path.join(res, RUN, "_groups", "moe.rack", rack, "summary.json"), group)
        store.update(None, ("moe.rack", rack, group))
        for name in names:
            pass
    for name in names:
        store.update({name: entries.get(name, {})},
                     ("dcgm", name, {"item": "dcgm", "group": name, "status": entries[name]["dcgm"]["status"],
                                     "hosts": [name], "run_id": RUN, "mode": "per-node"}))
    write_json(os.path.join(res, RUN, "run.json"), {
        "run_id": RUN, "pid": 1, "status": "fail", "started_at": "2026-11-12T09:30:00Z",
        "finished_at": "2026-11-12T10:30:00Z", "items": {"dcgm": {"hosts": names}, "moe.rack": {"hosts": names}}})
    return ws, cluster, tests
