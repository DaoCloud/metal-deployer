#!/bin/bash
set -euo pipefail

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"
BUILD_DIR="${BUILD_DIR:-/tmp/gpu-rdma-test-tools}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gpu-rdma-test-tools}"
CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"

mkdir -p "$BUILD_DIR" "$INSTALL_DIR/bin"

log() {
    echo "[install_test_tools] $*" >&2
}

has_nvidia_gpu() {
    lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'
}

has_rdma_adapter() {
    lspci | grep -qiE 'mellanox|nvidia.*connectx|bluefield'
}

detect_mpi_home() {
    local include_dir

    if command -v mpicc >/dev/null 2>&1; then
        include_dir="$(mpicc --showme:compile 2>/dev/null | tr ' ' '\n' | sed -n 's/^-I//p' | while read -r dir; do
            [ -f "${dir}/mpi.h" ] && echo "$dir" && break
        done)"
        if [ -n "$include_dir" ]; then
            dirname "$include_dir"
            return 0
        fi
    fi

    if [ -f /usr/lib/x86_64-linux-gnu/openmpi/include/mpi.h ]; then
        echo /usr/lib/x86_64-linux-gnu/openmpi
    fi
}

detect_nccl_tests_gencode() {
    local arch

    if [ -n "${NCCL_TESTS_NVCC_GENCODE:-}" ]; then
        echo "$NCCL_TESTS_NVCC_GENCODE"
        return 0
    fi

    arch="$(detect_cuda_build_arch)"
    [ -n "$arch" ] || return 0

    echo "-gencode=arch=compute_${arch},code=sm_${arch}"
}

detect_cuda_build_arch() {
    local cap major minor arch cuda_major

    if [ -n "${CUDA_BUILD_ARCH:-}" ]; then
        echo "$CUDA_BUILD_ARCH"
        return 0
    fi

    if ! command -v nvidia-smi >/dev/null 2>&1; then
        return 0
    fi

    cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1 | tr -d '[:space:]')"
    [ -n "$cap" ] || return 0
    major="${cap%%.*}"
    minor="${cap#*.}"
    [ -n "$major" ] && [ -n "$minor" ] || return 0
    arch="${major}${minor}"

    cuda_major="$(cuda_major_version)"
    if [ -n "$cuda_major" ] && [ "$cuda_major" -ge 13 ] && [ "$arch" -lt 75 ]; then
        log "Installed GPU is sm_${arch}, but CUDA ${cuda_major} cannot build that target; using sm_75 for build-only validation."
        echo "75"
        return 0
    fi

    echo "$arch"
}

cuda_major_version() {
    if ! command -v nvcc >/dev/null 2>&1; then
        return 0
    fi

    nvcc --version | sed -n 's/.*release \([0-9][0-9]*\)\..*/\1/p' | head -n 1
}

build_nccl_tests() {
    local archive mpi_home nvcc_gencode
    archive=$(find "$PACKAGE_DIR" -maxdepth 1 -name 'nccl-tests*.tar.gz' | head -n 1)
    [ -n "$archive" ] || return 0
    if [ "${SKIP_NCCL_TESTS:-0}" = "1" ]; then
        log "Skip nccl-tests: SKIP_NCCL_TESTS=1."
        return 0
    fi
    if ! has_nvidia_gpu; then
        log "Skip nccl-tests: no NVIDIA GPU detected."
        return 0
    fi
    if [ ! -f /usr/include/nccl.h ] && [ ! -f "${CUDA_HOME}/include/nccl.h" ]; then
        log "Skip nccl-tests: nccl.h is missing."
        return 0
    fi

    rm -rf "$BUILD_DIR/nccl-tests"
    mkdir -p "$BUILD_DIR/nccl-tests"
    tar -xzf "$archive" -C "$BUILD_DIR/nccl-tests" --strip-components=1
    mpi_home="$(detect_mpi_home)"
    if [ -z "$mpi_home" ] || [ ! -f "${mpi_home}/include/mpi.h" ]; then
        log "Skip nccl-tests: MPI headers are missing."
        return 0
    fi
    nvcc_gencode="$(detect_nccl_tests_gencode)"
    if [ -n "$nvcc_gencode" ]; then
        make -C "$BUILD_DIR/nccl-tests" MPI=1 MPI_HOME="$mpi_home" CUDA_HOME="$CUDA_HOME" NVCC_GENCODE="$nvcc_gencode"
    else
        make -C "$BUILD_DIR/nccl-tests" MPI=1 MPI_HOME="$mpi_home" CUDA_HOME="$CUDA_HOME"
    fi
    find "$BUILD_DIR/nccl-tests/build" -maxdepth 1 -type f -perm -111 -exec cp {} "$INSTALL_DIR/bin/" \;
}

build_nvbandwidth() {
    local archive cuda_arch
    archive=$(find "$PACKAGE_DIR" -maxdepth 1 -name 'nvbandwidth*.tar.gz' | head -n 1)
    [ -n "$archive" ] || return 0
    if [ "${SKIP_NVBANDWIDTH:-0}" = "1" ]; then
        log "Skip nvbandwidth: SKIP_NVBANDWIDTH=1."
        return 0
    fi
    if ! has_nvidia_gpu; then
        log "Skip nvbandwidth: no NVIDIA GPU detected."
        return 0
    fi

    rm -rf "$BUILD_DIR/nvbandwidth"
    mkdir -p "$BUILD_DIR/nvbandwidth"
    tar -xzf "$archive" -C "$BUILD_DIR/nvbandwidth" --strip-components=1
    cuda_arch="$(detect_cuda_build_arch)"
    if [ -n "$cuda_arch" ]; then
        sed -i "s/^set(supported_archs .*/set(supported_archs \"${cuda_arch}\")/" "$BUILD_DIR/nvbandwidth/CMakeLists.txt"
        cmake -S "$BUILD_DIR/nvbandwidth" -B "$BUILD_DIR/nvbandwidth/build" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCUDAToolkit_ROOT="$CUDA_HOME" \
            -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" \
            -DCMAKE_CUDA_ARCHITECTURES="$cuda_arch"
    else
        cmake -S "$BUILD_DIR/nvbandwidth" -B "$BUILD_DIR/nvbandwidth/build" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCUDAToolkit_ROOT="$CUDA_HOME" \
            -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc"
    fi
    cmake --build "$BUILD_DIR/nvbandwidth/build" -j"$(nproc)"
    find "$BUILD_DIR/nvbandwidth/build" -type f -perm -111 -name 'nvbandwidth*' -exec cp {} "$INSTALL_DIR/bin/" \;
}

build_perftest() {
    local archive
    archive=$(find "$PACKAGE_DIR" -maxdepth 1 -name 'perftest*.tar.gz' | head -n 1)
    [ -n "$archive" ] || return 0
    if [ "${SKIP_PERFTEST:-0}" = "1" ]; then
        log "Skip perftest: SKIP_PERFTEST=1."
        return 0
    fi
    if ! has_rdma_adapter; then
        log "No RDMA adapter detected; building perftest for artifact validation only."
    fi

    rm -rf "$BUILD_DIR/perftest"
    mkdir -p "$BUILD_DIR/perftest"
    tar -xzf "$archive" -C "$BUILD_DIR/perftest" --strip-components=1
    if [ "$(cuda_major_version)" = "13" ]; then
        sed -i 's/cuCtxCreate(\([^,]*\), \([^,]*\), \([^)]*\))/cuCtxCreate(\1, NULL, \2, \3)/' "$BUILD_DIR/perftest/src/cuda_memory.c"
    fi
    cd "$BUILD_DIR/perftest"
    ./autogen.sh
    ./configure CUDA_H_PATH="${CUDA_HOME}/include/cuda.h"
    make -j"$(nproc)"
    make install
}

build_cuda_bandwidthtest() {
    local archive sample_dir cuda_arch
    archive=$(find "$PACKAGE_DIR" -maxdepth 1 -name 'cuda-samples*.tar.gz' | head -n 1)
    [ -n "$archive" ] || return 0
    if ! has_nvidia_gpu; then
        log "Skip CUDA bandwidthTest: no NVIDIA GPU detected."
        return 0
    fi

    rm -rf "$BUILD_DIR/cuda-samples"
    mkdir -p "$BUILD_DIR/cuda-samples"
    tar -xzf "$archive" -C "$BUILD_DIR/cuda-samples" --strip-components=1
    sample_dir="$BUILD_DIR/cuda-samples/Samples/1_Utilities/bandwidthTest"
    if [ ! -d "$sample_dir" ]; then
        sample_dir="$BUILD_DIR/cuda-samples/Samples/1_Utilities/deviceQuery"
    fi
    if [ ! -d "$sample_dir" ]; then
        log "Skip CUDA sample: neither bandwidthTest nor deviceQuery exists."
        return 0
    fi
    cuda_arch="$(detect_cuda_build_arch)"
    if [ -f "$sample_dir/Makefile" ] && [ -n "$cuda_arch" ]; then
        make -C "$sample_dir" CUDA_PATH="$CUDA_HOME" SMS="$cuda_arch" -j"$(nproc)"
    elif [ -f "$sample_dir/Makefile" ]; then
        make -C "$sample_dir" CUDA_PATH="$CUDA_HOME" -j"$(nproc)"
    elif [ -f "$sample_dir/CMakeLists.txt" ] && [ -n "$cuda_arch" ]; then
        cmake -S "$sample_dir" -B "$sample_dir/build" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCUDAToolkit_ROOT="$CUDA_HOME" \
            -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc" \
            -DCMAKE_CUDA_ARCHITECTURES="$cuda_arch"
        cmake --build "$sample_dir/build" -j"$(nproc)"
        find "$sample_dir/build" -maxdepth 1 -type f -perm -111 -exec cp {} "$INSTALL_DIR/bin/" \;
        return 0
    elif [ -f "$sample_dir/CMakeLists.txt" ]; then
        cmake -S "$sample_dir" -B "$sample_dir/build" \
            -DCMAKE_BUILD_TYPE=Release \
            -DCUDAToolkit_ROOT="$CUDA_HOME" \
            -DCMAKE_CUDA_COMPILER="$CUDA_HOME/bin/nvcc"
        cmake --build "$sample_dir/build" -j"$(nproc)"
        find "$sample_dir/build" -maxdepth 1 -type f -perm -111 -exec cp {} "$INSTALL_DIR/bin/" \;
        return 0
    fi
    find "$sample_dir" -maxdepth 1 -type f -perm -111 -exec cp {} "$INSTALL_DIR/bin/" \;
}

build_nccl_tests
build_nvbandwidth
build_perftest
build_cuda_bandwidthtest

cat >/etc/profile.d/gpu-rdma-test-tools.sh <<EOF
export PATH=${INSTALL_DIR}/bin:\$PATH
EOF
