#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"
BUILD_DIR="${BUILD_DIR:-/tmp/gdrcopy-build}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local}"
MODULE_LOAD_FILE="${MODULE_LOAD_FILE:-/etc/modules-load.d/gdrcopy.conf}"

log() {
    echo "[install_gdrcopy] $*" >&2
}

has_nvidia_gpu() {
    lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'
}

archive="$(find "$PACKAGE_DIR" -maxdepth 1 -name 'gdrcopy*.tar.gz' | head -n 1)"
[ -n "$archive" ] || {
    log "Skip GDRCopy: source archive is missing."
    exit 0
}

if ! has_nvidia_gpu; then
    log "Skip GDRCopy: no NVIDIA GPU detected."
    exit 0
fi

if [ ! -d "/lib/modules/$(uname -r)/build" ]; then
    log "Skip GDRCopy: kernel headers are missing for $(uname -r)."
    exit 0
fi

if [ ! -x "${CUDA_HOME}/bin/nvcc" ]; then
    log "Skip GDRCopy tests: CUDA compiler is missing at ${CUDA_HOME}/bin/nvcc."
fi

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
tar -xzf "$archive" -C "$BUILD_DIR" --strip-components=1

make -C "$BUILD_DIR" CUDA="$CUDA_HOME" driver
make -C "$BUILD_DIR" CUDA="$CUDA_HOME" lib

if [ -x "${CUDA_HOME}/bin/nvcc" ]; then
    make -C "$BUILD_DIR" CUDA="$CUDA_HOME" exes
fi

make -C "$BUILD_DIR" CUDA="$CUDA_HOME" drv_install
make -C "$BUILD_DIR" CUDA="$CUDA_HOME" prefix="$INSTALL_PREFIX" lib_install

if [ -x "${CUDA_HOME}/bin/nvcc" ]; then
    make -C "$BUILD_DIR" CUDA="$CUDA_HOME" prefix="$INSTALL_PREFIX" exes_install
fi

depmod -a
modprobe nvidia_peermem || true
modprobe gdrdrv

ldconfig
modinfo gdrdrv >/dev/null
grep -q '^gdrdrv ' /proc/modules

major="$(awk '$2 == "gdrdrv" {print $1}' /proc/devices)"
if [ -n "$major" ]; then
    rm -f /dev/gdrdrv
    mknod /dev/gdrdrv c "$major" 0
    chmod 0666 /dev/gdrdrv
fi

printf 'gdrdrv\n' > "$MODULE_LOAD_FILE"

log "GDRCopy installed and gdrdrv loaded."
