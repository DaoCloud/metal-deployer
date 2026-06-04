#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"
INSTALL_DIR="${INSTALL_DIR:-/opt/hpcx}"

shopt -s nullglob
archives=("$PACKAGE_DIR"/hpcx*.tbz "$PACKAGE_DIR"/hpcx*.tar.gz)
[ "${#archives[@]}" -gt 0 ] || exit 0

tar_args=(-xf "${archives[0]}")
if [[ "${archives[0]}" == *.tbz ]]; then
  if command -v lbzip2 >/dev/null 2>&1; then
    tar_args=(-I lbzip2 -xf "${archives[0]}")
  elif command -v bzip2 >/dev/null 2>&1; then
    tar_args=(-I bzip2 -xf "${archives[0]}")
  else
    echo "HPC-X archive requires lbzip2 or bzip2 to extract: ${archives[0]}" >&2
    exit 2
  fi
fi

rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

tar "${tar_args[@]}" -C "$tmp_dir"

hpcx_root=""
if [ -f "$tmp_dir/hpcx-init.sh" ]; then
  hpcx_root="$tmp_dir"
else
  hpcx_root="$(find "$tmp_dir" -mindepth 1 -name 'hpcx-init.sh' -printf '%h\n' | head -n 1)"
fi

if [ -z "$hpcx_root" ]; then
  echo "HPC-X archive missing hpcx-init.sh: ${archives[0]}" >&2
  exit 2
fi

cp -a "$hpcx_root"/. "$INSTALL_DIR"/

cat >/etc/profile.d/hpcx.sh <<EOF
export HPCX_HOME=${INSTALL_DIR}
if [ -f "\${HPCX_HOME}/hpcx-init.sh" ]; then
  # shellcheck disable=SC1090
  source "\${HPCX_HOME}/hpcx-init.sh"
  hpcx_load || true
fi
EOF
