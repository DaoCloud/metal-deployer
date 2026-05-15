#!/bin/bash
#
# Copyright 2024 Authors of metal-deployer
# SPDX-License-Identifier: Apache-2.0
#
# Build and install GPU Burn - GPU stress test tool
# Supports CUDA 13.2, adapted for Ubuntu 24.04

set -o errexit
set -o nounset
set -o pipefail

# Configuration
GPU_BURN_VERSION="${GPU_BURN_VERSION:-1.0}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local/bin}"
INSTALL_LIB_DIR="${INSTALL_LIB_DIR:-/usr/local/lib/gpu-burn}"
BUILD_DIR="${BUILD_DIR:-/tmp/gpu-burn-build}"
SOURCE_DIR="${SOURCE_DIR:-/opt/resource/packages}"

LOG_FILE="${LOG_FILE:-}"

# Logging function
log() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "${line}"
    if [ -n "${LOG_FILE}" ]; then
        echo "${line}" >> "${LOG_FILE}"
    fi
}

run_logged() {
    if [ -n "${LOG_FILE}" ]; then
        "$@" 2>&1 | tee -a "${LOG_FILE}"
    else
        "$@"
    fi
}

detect_cuda_compute() {
    local cap

    if [ -n "${GPU_BURN_COMPUTE:-}" ]; then
        echo "$GPU_BURN_COMPUTE"
        return 0
    fi

    if command -v nvidia-smi >/dev/null 2>&1; then
        cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1 | tr -d '[:space:].')"
        if [ -n "$cap" ]; then
            echo "$cap"
            return 0
        fi
    fi

    echo "75"
}

log "=========================================="
log "Start installing GPU Burn"
log "Version: ${GPU_BURN_VERSION}"
log "CUDA: ${CUDA_HOME}"
log "Install path: ${INSTALL_PREFIX}"
log "=========================================="

# Check CUDA environment
if [ ! -d "${CUDA_HOME}" ]; then
    log "❌ Error: CUDA not found, Path ${CUDA_HOME} does not exist"
    log "Please ensure CUDA 13.2 is correctly installed"
    exit 1
fi

# Check CUDA version
CUDA_VERSION=$(${CUDA_HOME}/bin/nvcc --version 2>/dev/null | grep "release" | awk '{print $5}' | cut -c1-4 || echo "unknown")
log "📦 CUDA version: ${CUDA_VERSION}"

# Clean and create build directory
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

# Find source package
GPU_BURN_TARBALL="${SOURCE_DIR}/gpu-burn-1.0.tar.gz"

if [ -f "${GPU_BURN_TARBALL}" ]; then
    log "📦 UseLocal source package: ${GPU_BURN_TARBALL}"
    tar xzf "${GPU_BURN_TARBALL}"
else
    log "❌ Error: Local GPU Burn source package not found: ${GPU_BURN_TARBALL}"
    if [ "${GPU_BURN_ALLOW_GIT_CLONE:-false}" != "true" ]; then
        log "Offline first-boot install requires ${GPU_BURN_TARBALL} in /opt/resource/packages."
        log "Run prepare so gpu-burn-1.0.tar.gz is packaged, set ENABLE_GPU_BURN_BUILD=false to skip, or set GPU_BURN_ALLOW_GIT_CLONE=true to allow network clone."
        exit 1
    fi

    log "🌐 GPU_BURN_ALLOW_GIT_CLONE=true; clone from GitHub..."
    git clone --depth=1 https://github.com/wilicc/gpu-burn.git "gpu-burn-${GPU_BURN_VERSION}" || {
        log "❌ GitHub clone failed, try mirror site..."
        git clone --depth=1 https://github.com/wilicc/gpu-burn.git "gpu-burn-${GPU_BURN_VERSION}"
    }
fi

# Enter source directory
GPU_BURN_SRC=$(find . -maxdepth 1 -type d -name "gpu-burn*" | head -n1)
if [ -z "${GPU_BURN_SRC}" ]; then
    log "❌ Error: Cannot find GPU Burn Source directory"
    exit 1
fi

cd "${GPU_BURN_SRC}"
log "📂 Enter source directory: $(pwd)"

# Check Makefile
if [ ! -f "Makefile" ]; then
    log "❌ Error: Makefile does not exist"
    exit 1
fi

# Modify Makefile to use correct CUDA path (if required)
log "🔧 Configure CUDA path..."
if grep -q "CUDAPATH" Makefile; then
    sed -i "s|^CUDAPATH.*|CUDAPATH ?= ${CUDA_HOME}|g" Makefile
elif grep -q "CUDA_PATH" Makefile; then
    sed -i "s|^CUDA_PATH.*|CUDA_PATH=${CUDA_HOME}|g" Makefile
elif grep -q "CUDA_HOME" Makefile; then
    sed -i "s|^CUDA_HOME.*|CUDA_HOME=${CUDA_HOME}|g" Makefile
fi

# Compile
log "🔨 Start compiling GPU Burn..."
make clean 2>/dev/null || true
run_logged make -j"$(nproc)" CUDAPATH="${CUDA_HOME}" COMPUTE="$(detect_cuda_compute)"

# Check build result
if [ -f "gpu_burn" ]; then
    GPU_BURN_BINARY="gpu_burn"
elif [ -f "gpu-burn" ]; then
    GPU_BURN_BINARY="gpu-burn"
else
    log "❌ Error: Build failed, gpu-burn binary not generated"
    exit 1
fi

if [ -f "compare.fatbin" ]; then
    GPU_BURN_KERNEL="compare.fatbin"
elif [ -f "compare.ptx" ]; then
    GPU_BURN_KERNEL="compare.ptx"
else
    log "❌ Error: Build failed, compare kernel artifact not generated"
    exit 1
fi

# Install
log "📦 Install gpu-burn to ${INSTALL_PREFIX}..."
mkdir -p "${INSTALL_PREFIX}" "${INSTALL_LIB_DIR}"
install -m 755 "${GPU_BURN_BINARY}" "${INSTALL_LIB_DIR}/gpu_burn"
install -m 644 "${GPU_BURN_KERNEL}" "${INSTALL_LIB_DIR}/${GPU_BURN_KERNEL}"
cat > "${INSTALL_PREFIX}/gpu-burn" <<EOF
#!/bin/bash
set -e
cd "${INSTALL_LIB_DIR}"
exec ./gpu_burn "\$@"
EOF
chmod +x "${INSTALL_PREFIX}/gpu-burn"

# Create convenience script
cat > "${INSTALL_PREFIX}/gpu-burn-test" << 'EOF'
#!/bin/bash
# GPU Burn quick test script
# Default test 60 seconds

DURATION="${1:-60}"
GPU_BURN_BIN="/usr/local/bin/gpu-burn"

if [ ! -f "${GPU_BURN_BIN}" ]; then
    echo "❌ gpu-burn not installed"
    exit 1
fi

echo "🔥 Start GPU stress test, Duration ${DURATION} seconds..."
echo "Press Ctrl+C to stop test"
"${GPU_BURN_BIN}" "${DURATION}"
EOF

chmod +x "${INSTALL_PREFIX}/gpu-burn-test"

# Verify installation
log "✅ Verify installation..."
"${INSTALL_PREFIX}/gpu-burn" --help 2>&1 | head -5 || true

# Clean
log "🧹 Clean build directory..."
cd /
rm -rf "${BUILD_DIR}"

log "=========================================="
log "✅ GPU Burn installation complete"
log "Install path: ${INSTALL_PREFIX}/gpu-burn"
log "Usage:"
log "  gpu-burn <seconds>     # Test for specified seconds"
log "  gpu-burn-test          # Quick test (default 60s)"
log "=========================================="
