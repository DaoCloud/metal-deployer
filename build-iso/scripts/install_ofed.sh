#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"
WORK_DIR="${WORK_DIR:-/tmp/mlnx-ofed-install}"

shopt -s nullglob
archives=("$PACKAGE_DIR"/MLNX_OFED_LINUX*.tgz "$PACKAGE_DIR"/mlnx-ofed*.tgz)
[ "${#archives[@]}" -gt 0 ] || exit 0

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"
tar -xzf "${archives[0]}" -C "$WORK_DIR"

installer=$(find "$WORK_DIR" -maxdepth 2 -type f \( -name mlnxofedinstall -o -name install.pl \) | head -n 1)
[ -n "$installer" ] || { echo "MLNX_OFED installer not found"; exit 1; }

"$installer" --without-fw-update --force
/etc/init.d/openibd restart || systemctl restart openibd || true
