# Copyright 2024 Authors of spidernet-io
# SPDX-License-Identifier: Apache-2.0

# Global build arguments - must be defined before first FROM
ARG ENV_BUILD_TOOLS_IMAGE_NAME=nvidia/cuda:12.8.1-devel-ubuntu22.04
ARG ENV_BASEIMAGE_FULL_NAME=nvidia/cuda:12.8.1-runtime-ubuntu22.04

#============= build tools ============

FROM ${ENV_BUILD_TOOLS_IMAGE_NAME} AS buildtools

ARG TARGETOS
ARG TARGETARCH

# Build configuration arguments
ARG ENV_BASEIMAGE_CUDA_VERISON=12.8
ARG ENV_DEEPEP_VERSION=v1.2.1
ARG ENV_DEEPGEMM_VERSION=v2.1.1.post3
ARG ENV_UCX_VERSION=v1.20.0
ARG ENV_NVSHMEM_VERSION=v3.4.5-0
ARG ENV_BUILD_AND_DOWNLOAD_PARALLEL=4
ARG ENV_GITHUB_ARTIFACTORY=github.com
ARG ENV_TORCH_CUDA_ARCH_LIST="9.0"
ARG ENV_NIXL_REPO=https://github.com/ai-dynamo/nixl.git
ARG ENV_NIXL_REF=main
ARG ENV_LIBFABRIC_VERSION=v1.21.0
ARG ENV_GDRCOPY_VERSION=v2.5.1
ARG ENV_ABSL_TAG=lts_2025_08_14
ARG ENV_GRPC_TAG=v1.73.0

# Set as environment variables for scripts
ENV ENV_BASEIMAGE_CUDA_VERISON=${ENV_BASEIMAGE_CUDA_VERISON}
ENV ENV_DEEPEP_VERSION=${ENV_DEEPEP_VERSION}
ENV ENV_DEEPGEMM_VERSION=${ENV_DEEPGEMM_VERSION}
ENV ENV_UCX_VERSION=${ENV_UCX_VERSION}
ENV ENV_NVSHMEM_VERSION=${ENV_NVSHMEM_VERSION}
ENV ENV_BUILD_AND_DOWNLOAD_PARALLEL=${ENV_BUILD_AND_DOWNLOAD_PARALLEL}
ENV ENV_GITHUB_ARTIFACTORY=${ENV_GITHUB_ARTIFACTORY}
ENV ENV_TORCH_CUDA_ARCH_LIST=${ENV_TORCH_CUDA_ARCH_LIST}
ENV ENV_NIXL_REPO=${ENV_NIXL_REPO}
ENV ENV_NIXL_REF=${ENV_NIXL_REF}
ENV ENV_LIBFABRIC_VERSION=${ENV_LIBFABRIC_VERSION}
ENV ENV_GDRCOPY_VERSION=${ENV_GDRCOPY_VERSION}
ENV ENV_ABSL_TAG=${ENV_ABSL_TAG}
ENV ENV_GRPC_TAG=${ENV_GRPC_TAG}

# Create output directories
RUN mkdir -p /buildDeepEP /buildDeepGEMM /buildUcxRootfs/usr/local /buildBenchRootfs/usr/local /opt/nvshmem

# Install build dependencies
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    autoconf \
    automake \
    libtool \
    m4 \
    make \
    pkg-config \
    ca-certificates \
    python-is-python3 \
    git \
    curl \
    unzip \
    build-essential \
    cmake \
    ninja-build \
    libibverbs-dev \
    librdmacm-dev \
    libnuma-dev \
    rdma-core \
    ibverbs-providers \
    ibverbs-utils \
    libibumad-dev \
    libgflags-dev \
    libgrpc-dev \
    libgrpc++-dev \
    libprotobuf-dev \
    libaio-dev \
    libhwloc-dev \
    protobuf-compiler-grpc \
    libcpprest-dev \
    libcurl4-openssl-dev \
    libssl-dev \
    uuid-dev \
    zlib1g-dev \
    libxml2-dev \
    pybind11-dev \
    libclang-dev \
    flex \
    bzip2 \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy build scripts
COPY /scripts/build-deepep.sh /build-deepep.sh
COPY /scripts/build-ucx.sh /build-ucx.sh

# Build GDRCopy userspace library for DeepEP/NIXL libfabric detection
RUN wget --tries=3 --waitretry=5 --timeout=30 --read-timeout=60 \
      "https://${ENV_GITHUB_ARTIFACTORY}/NVIDIA/gdrcopy/archive/refs/tags/${ENV_GDRCOPY_VERSION}.tar.gz" \
      -O /tmp/gdrcopy.tar.gz \
    && tar xzf /tmp/gdrcopy.tar.gz -C /tmp \
    && rm /tmp/gdrcopy.tar.gz \
    && cd /tmp/gdrcopy-* \
    && make -j${ENV_BUILD_AND_DOWNLOAD_PARALLEL} lib \
    && make lib_install DESTDIR=/ prefix=/usr/local \
    && make lib_install DESTDIR=/buildBenchRootfs prefix=/usr/local \
    && ldconfig \
    && cd / \
    && rm -rf /tmp/gdrcopy-*

# Build DeepEP and DeepGEMM
RUN chmod +x /build-deepep.sh && /build-deepep.sh

# Build UCX with CUDA and RDMA support
RUN chmod +x /build-ucx.sh && /build-ucx.sh

# Build modern Abseil and gRPC for NIXL/NIXLBench
RUN git clone --depth 1 --branch "${ENV_ABSL_TAG}" https://${ENV_GITHUB_ARTIFACTORY}/abseil/abseil-cpp.git /tmp/abseil-cpp \
    && cmake -S /tmp/abseil-cpp -B /tmp/abseil-cpp/build \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_CXX_STANDARD=20 \
      -DBUILD_SHARED_LIBS=ON \
      -DABSL_PROPAGATE_CXX_STD=ON \
      -DABSL_ENABLE_INSTALL=ON \
    && cmake --build /tmp/abseil-cpp/build --parallel ${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && cmake --install /tmp/abseil-cpp/build \
    && DESTDIR=/buildBenchRootfs cmake --install /tmp/abseil-cpp/build \
    && ldconfig \
    && rm -rf /tmp/abseil-cpp

RUN git clone --recurse-submodules -b "${ENV_GRPC_TAG}" --depth 1 --shallow-submodules https://${ENV_GITHUB_ARTIFACTORY}/grpc/grpc.git /tmp/grpc \
    && cmake -S /tmp/grpc -B /tmp/grpc/cmake/build \
      -DgRPC_INSTALL=ON \
      -DgRPC_BUILD_TESTS=OFF \
      -DBUILD_SHARED_LIBS=ON \
      -DCMAKE_CXX_STANDARD=20 \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_PREFIX_PATH=/usr/local \
      -Dabsl_DIR=/usr/local/lib/cmake/absl \
      -DgRPC_SSL_PROVIDER=package \
      -DgRPC_ABSL_PROVIDER=package \
      -DgRPC_PROTOBUF_PROVIDER=module \
      -DgRPC_ZLIB_PROVIDER=package \
    && cmake --build /tmp/grpc/cmake/build --parallel ${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && cmake --install /tmp/grpc/cmake/build \
    && DESTDIR=/buildBenchRootfs cmake --install /tmp/grpc/cmake/build \
    && ldconfig \
    && rm -rf /tmp/grpc

# Build libfabric for NIXL libfabric backend
RUN wget --tries=3 --waitretry=5 --timeout=30 --read-timeout=60 \
      "https://${ENV_GITHUB_ARTIFACTORY}/ofiwg/libfabric/releases/download/${ENV_LIBFABRIC_VERSION}/libfabric-${ENV_LIBFABRIC_VERSION#v}.tar.bz2" \
      -O /tmp/libfabric.tar.bz2 \
    && tar xjf /tmp/libfabric.tar.bz2 -C /tmp \
    && rm /tmp/libfabric.tar.bz2 \
    && cd /tmp/libfabric-* \
    && ./configure --prefix=/usr/local \
      --disable-verbs \
      --disable-psm3 \
      --disable-opx \
      --disable-usnic \
      --disable-rstream \
      --enable-efa \
      --with-cuda=/usr/local/cuda \
      --enable-cuda-dlopen \
      --enable-gdrcopy-dlopen \
    && make -j${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && make install DESTDIR=/buildBenchRootfs \
    && cd / \
    && rm -rf /tmp/libfabric-*

# Install UCX/libfabric into build stage for NIXL detection
RUN cp -a /buildUcxRootfs/usr/local/. /usr/local/ \
    && cp -a /buildBenchRootfs/usr/local/. /usr/local/ \
    && ldconfig

# Build etcd-cpp-api for NIXLBench metadata coordination
RUN git clone --depth 1 https://${ENV_GITHUB_ARTIFACTORY}/etcd-cpp-apiv3/etcd-cpp-apiv3.git /tmp/etcd-cpp-apiv3 \
    && cd /tmp/etcd-cpp-apiv3 \
    && sed -i '/^find_dependency(cpprestsdk)$/d' etcd-cpp-api-config.in.cmake \
    && cmake -S . -B build \
      -DBUILD_ETCD_CORE_ONLY=ON \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_PREFIX_PATH=/usr/local \
    && cmake --build build --parallel ${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && cmake --install build \
    && DESTDIR=/buildBenchRootfs cmake --install build \
    && cd / \
    && rm -rf /tmp/etcd-cpp-apiv3

# Build NIXL and NIXLBench
RUN python3 -m pip install --no-cache-dir --upgrade \
      meson \
      pybind11 \
      patchelf \
      pyYAML \
      click \
      tabulate \
      tomlkit \
    && git clone --depth 1 --branch "${ENV_NIXL_REF}" "${ENV_NIXL_REPO}" /tmp/nixl \
    && cd /tmp/nixl \
    && meson setup build --prefix=/usr/local/nixl --buildtype=release \
    && ninja -C build -j${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && DESTDIR=/buildBenchRootfs ninja -C build install \
    && mkdir -p /usr/local/nixl \
    && cp -a /buildBenchRootfs/usr/local/nixl/. /usr/local/nixl/ \
    && echo "/usr/local/nixl/lib/$(uname -m)-linux-gnu" > /etc/ld.so.conf.d/nixl.conf \
    && echo "/usr/local/nixl/lib/$(uname -m)-linux-gnu/plugins" >> /etc/ld.so.conf.d/nixl.conf \
    && ldconfig \
    && cd /tmp/nixl/benchmark/nixlbench \
    && meson setup build -Dnixl_path=/usr/local/nixl/ -Dprefix=/usr/local/nixlbench --buildtype=release \
    && ninja -C build -j${ENV_BUILD_AND_DOWNLOAD_PARALLEL} \
    && DESTDIR=/buildBenchRootfs ninja -C build install \
    && cd / \
    && rm -rf /tmp/nixl

#========== root image ==============
FROM ${ENV_BASEIMAGE_FULL_NAME} AS rootfs

ARG TARGETOS
ARG TARGETARCH

ARG GIT_COMMIT_VERSION
ENV GIT_COMMIT_VERSION=${GIT_COMMIT_VERSION}
ARG GIT_COMMIT_TIME
ENV GIT_COMMIT_TIME=${GIT_COMMIT_TIME}
ARG VERSION
ENV VERSION=${VERSION}

# CUDA and system configuration
ARG ENV_BASEIMAGE_CUDA_VERISON=12.8
ARG ENV_BASEIMAGE_OS_VERISON=ubuntu22.04
ENV ENV_BASEIMAGE_CUDA_VERISON=${ENV_BASEIMAGE_CUDA_VERISON}
ENV ENV_BASEIMAGE_OS_VERISON=${ENV_BASEIMAGE_OS_VERISON}

# Copy tools scripts
COPY /tools /tools

# Copy built artifacts from buildtools stage
COPY --from=buildtools /opt/DeepEP            /opt/DeepEP
COPY --from=buildtools /opt/DeepGEMM          /opt/DeepGEMM
COPY --from=buildtools /buildDeepEP           /buildDeepEP
COPY --from=buildtools /buildDeepGEMM         /buildDeepGEMM
COPY --from=buildtools /buildUcxRootfs/usr/local/ /usr/local/
COPY --from=buildtools /buildBenchRootfs/usr/local/ /usr/local/
COPY --from=buildtools /opt/nvshmem          /opt/nvshmem

# Install system dependencies
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    xz-utils \
    curl \
    etcd-client \
    etcd-server \
    git \
    ibverbs-providers \
    ibverbs-utils \
    libaio-dev \
    libcpprest-dev \
    libgflags-dev \
    libgrpc++-dev \
    libhwloc15 \
    libibumad-dev \
    libibverbs-dev \
    libprotobuf-dev \
    librdmacm-dev \
    jq \
    ssh \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH="$(uname -m)" \
    && echo "/usr/local/nixl/lib/${ARCH}-linux-gnu" > /etc/ld.so.conf.d/nixl.conf \
    && echo "/usr/local/nixl/lib/${ARCH}-linux-gnu/plugins" >> /etc/ld.so.conf.d/nixl.conf \
    && ldconfig

# Install Python dependencies for DeepEP
RUN CUDA_SHORT="$(echo "${ENV_BASEIMAGE_CUDA_VERISON}" | cut -d. -f1,2)" \
    && CU_VER="$(echo "${CUDA_SHORT}" | tr -d .)" \
    && pip3 install --no-cache-dir numpy packaging \
    && pip3 install --no-cache-dir torch --index-url "https://download.pytorch.org/whl/cu${CU_VER}" \
    && python3 -c "import sys, torch; expected='${CUDA_SHORT}'; actual=torch.version.cuda; sys.exit(f'PyTorch CUDA version mismatch: expected {expected}, got {actual}') if actual != expected else None"

# Move tools to system path and make executable
RUN chmod +x /tools/* && mv /tools/* /usr/sbin && rm -rf /tools

# Set library paths
ENV DEEPEP_DIR=/opt/DeepEP
ENV DEEPGEMM_DIR=/opt/DeepGEMM
ENV NVSHMEM_DIR=/opt/nvshmem
ENV LD_LIBRARY_PATH=/opt/nvshmem/lib:/usr/local/lib:/usr/local/nixlbench/lib:/opt/DeepEP:${LD_LIBRARY_PATH}
ENV PATH=/usr/local/nixlbench/bin:/usr/local/nixl/bin:/usr/local/bin:${PATH}
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# Set default workdir
WORKDIR /

# Default command
CMD ["/bin/bash"]
