#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"
INSTALL_DIR="${INSTALL_DIR:-/opt/hpcx}"

shopt -s nullglob
archives=("$PACKAGE_DIR"/hpcx*.tbz "$PACKAGE_DIR"/hpcx*.tar.gz)
[ "${#archives[@]}" -gt 0 ] || exit 0

rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tar -xf "${archives[0]}" -C "$INSTALL_DIR" --strip-components=1

cat >/etc/profile.d/hpcx.sh <<EOF
export HPCX_HOME=${INSTALL_DIR}
if [ -f "\${HPCX_HOME}/hpcx-init.sh" ]; then
  source "\${HPCX_HOME}/hpcx-init.sh"
  hpcx_load
fi
EOF
