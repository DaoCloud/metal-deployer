"""Local state under the results directory."""

import json
import os


def nic_path(results_dir, hostname):
    return os.path.join(results_dir, "nic", hostname + ".json")


def load_nic(results_dir, hostname):
    """NIC discovery result of one host (written by "mtest nic discover"), or None."""
    path = nic_path(results_dir, hostname)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    """Write atomically so readers never see half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
