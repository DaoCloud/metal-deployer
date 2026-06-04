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

dump_module_diagnostics() {
    log "kernel: $(uname -r)"
    log "nvidia packages:"
    dpkg -l | awk '$1=="ii" && $2 ~ /^(nvidia|cuda|libnvidia)/ {print "  " $2, $3}' >&2 || true
    log "rdma/doca packages:"
    dpkg -l | awk '$1=="ii" && $2 ~ /^(doca|rdma|ibverbs|infiniband|mlnx|ofed|libibverbs|librdmacm)/ {print "  " $2, $3}' >&2 || true
    log "loaded modules:"
    lsmod | awk '$1 ~ /^(nvidia|nvidia_peermem|gdrdrv|mlx5|ib_|rdma|nv_peer_mem)/ {print "  " $0}' >&2 || true
    log "NVIDIA/RDMA PCI devices:"
    lspci -nn | grep -iE 'nvidia|mellanox|connectx|bluefield' >&2 || true
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
dump_module_diagnostics

# nvidia_peermem implements the legacy InfiniBand PeerDirect
# (ib_register_peer_memory_client) path. Recent DOCA/MLNX OFED (>= 25.x) and
# mainline kernels no longer export that API; GPUDirect RDMA is provided through
# the dma-buf path (ib_umem_dmabuf_get) instead. On those stacks nvidia_peermem
# returns EINVAL and is simply not needed, so treat its load as best-effort and
# do not fail the GDRCopy install (which only requires gdrdrv).
peermem_loaded=0
if modprobe nvidia_peermem 2>/dev/null; then
    peermem_loaded=1
    log "nvidia_peermem loaded (legacy PeerDirect GPUDirect RDMA path)."
else
    log "Skip nvidia_peermem: PeerDirect API unavailable (modern OFED/kernel uses dma-buf GPUDirect RDMA); not required."
fi
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

if [ "$peermem_loaded" -eq 1 ]; then
    printf 'nvidia_peermem\ngdrdrv\n' > "$MODULE_LOAD_FILE"
    log "GDRCopy installed; nvidia_peermem and gdrdrv loaded."
else
    printf 'gdrdrv\n' > "$MODULE_LOAD_FILE"
    log "GDRCopy installed; gdrdrv loaded (nvidia_peermem skipped, dma-buf GPUDirect RDMA in use)."
fi
