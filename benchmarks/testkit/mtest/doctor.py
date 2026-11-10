"""Check local dependencies of mtest."""

import shutil
import subprocess
import sys


def _version(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = p.stdout.decode(errors="replace").strip().splitlines()
    return out[0] if out else ""


def checks():
    """Return rows of (name, required, ok, detail, impact)."""
    rows = []
    py_ok = sys.version_info >= (3, 8)
    rows.append(("python", True, py_ok, sys.version.split()[0], "" if py_ok else "need Python >= 3.8"))
    try:
        import yaml
        rows.append(("pyyaml", True, True, yaml.__version__, ""))
    except ImportError:
        rows.append(("pyyaml", True, False, "missing", "pip install PyYAML"))
    ssh = shutil.which("ssh")
    rows.append(("ssh", True, bool(ssh), _version(["ssh", "-V"]) if ssh else "missing",
                 "" if ssh else "install openssh-client"))
    try:
        import openpyxl
        rows.append(("openpyxl", False, True, openpyxl.__version__, ""))
    except ImportError:
        rows.append(("openpyxl", False, False, "missing", "mtest import reads CSV only (pip install openpyxl)"))
    keygen = shutil.which("ssh-keygen")
    rows.append(("ssh-keygen", False, bool(keygen), keygen or "missing",
                 "" if keygen else "mpi-mode items (imex, sharp) cannot run"))
    rsync = shutil.which("rsync")
    rows.append(("rsync", False, bool(rsync), rsync or "missing", "" if rsync else "results are copied with ssh + tar"))
    sb = shutil.which("sb")
    rows.append(("sb", False, bool(sb), sb or "missing",
                 "" if sb else "superbench items run sb on the rank 0 host (runner: auto)"))
    return rows


def run():
    rows = checks()
    width = max(len(r[0]) for r in rows)
    failed = False
    for name, required, ok, detail, impact in rows:
        mark = "ok" if ok else ("MISSING" if required else "missing")
        line = "{:<{w}}  {:<8} {}".format(name, mark, detail, w=width)
        if impact:
            line += "  -> " + impact
        print(line)
        failed = failed or (required and not ok)
    return 1 if failed else 0
