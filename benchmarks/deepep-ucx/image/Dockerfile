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
ARG ENV_TORCH_CUDA_ARCH_LIST="7.0;7.5;8.0;8.6;9.0"

# Set as environment variables for scripts
ENV ENV_BASEIMAGE_CUDA_VERISON=${ENV_BASEIMAGE_CUDA_VERISON}
ENV ENV_DEEPEP_VERSION=${ENV_DEEPEP_VERSION}
ENV ENV_DEEPGEMM_VERSION=${ENV_DEEPGEMM_VERSION}
ENV ENV_UCX_VERSION=${ENV_UCX_VERSION}
ENV ENV_NVSHMEM_VERSION=${ENV_NVSHMEM_VERSION}
ENV ENV_BUILD_AND_DOWNLOAD_PARALLEL=${ENV_BUILD_AND_DOWNLOAD_PARALLEL}
ENV ENV_GITHUB_ARTIFACTORY=${ENV_GITHUB_ARTIFACTORY}
ENV ENV_TORCH_CUDA_ARCH_LIST=${ENV_TORCH_CUDA_ARCH_LIST}

# Create output directories
RUN mkdir -p /buildDeepEP /buildDeepGEMM /buildUcxRootfs/usr/local /opt/nvshmem

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
    && rm -rf /var/lib/apt/lists/*

# Copy build scripts
COPY /scripts/build-deepep.sh /build-deepep.sh
COPY /scripts/build-ucx.sh /build-ucx.sh

# Build DeepEP and DeepGEMM
RUN chmod +x /build-deepep.sh && /build-deepep.sh

# Build UCX with CUDA and RDMA support
RUN chmod +x /build-ucx.sh && /build-ucx.sh

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
COPY --from=buildtools /opt/nvshmem          /opt/nvshmem

# Install system dependencies
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    xz-utils \
    curl \
    git \
    jq \
    ssh \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

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
ENV LD_LIBRARY_PATH=/opt/nvshmem/lib:/usr/local/lib:/opt/DeepEP:${LD_LIBRARY_PATH}
ENV PATH=/usr/local/bin:${PATH}

# Set default workdir
WORKDIR /

# Default command
CMD ["/bin/bash"]
