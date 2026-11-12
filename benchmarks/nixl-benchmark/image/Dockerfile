# syntax=docker/dockerfile:1.7
#
# NIXL + NIXLBench image. One Dockerfile for every architecture and scenario: all versions and
# switches are build arguments, and profiles/<name>.env holds the values of one scenario.
# Build with "make build-nixl-benchmark NIXL_PROFILE=<name>" (see README.md); an environment
# variable of the same name overrides the profile value.
#
# NIXL_PLUGINS selects the NIXL plugins (meson enable_plugins). It also decides what else is built:
#   LIBFABRIC     libfabric (verbs + EFA providers) is built from source
#   GDS / GDS_MT  the runtime image gets libcufile
# A plugin that is listed but cannot be built fails the build.

ARG BASE_BUILD_IMAGE=nvidia/cuda:13.0.2-devel-ubuntu24.04
ARG BASE_RUNTIME_IMAGE=nvidia/cuda:13.0.2-runtime-ubuntu24.04

#============= build ============
FROM ${BASE_BUILD_IMAGE} AS build

ARG GITHUB_URL=https://github.com
ARG NIXL_REPO=https://github.com/ai-dynamo/nixl.git
ARG NIXL_REF=v1.5.2
ARG NIXL_PLUGINS=UCX,LIBFABRIC,GDS,GDS_MT,POSIX
# CUDA SM targets of the NIXL CUDA code, e.g. "100,103" (B200/B300) or "103" (GB300); auto = upstream default
ARG NIXL_CUDA_ARCH=auto
ARG UCX_VERSION=v1.20.0
ARG LIBFABRIC_VERSION=v1.21.0
ARG GDRCOPY_VERSION=v2.5.1
ARG ABSL_TAG=lts_2025_08_14
ARG GRPC_TAG=v1.73.0
# etcd-cpp-apiv3 master (2025-03-28); upstream nixlbench builds master too, a commit keeps builds reproducible
ARG ETCD_CPP_REF=7c6e714f188f9576e25e0350cac4181139eec23e
# 0: number of CPUs
ARG BUILD_JOBS=0

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        autoconf automake libtool m4 make pkg-config build-essential cmake ninja-build \
        git curl wget ca-certificates bzip2 unzip flex \
        python3 python3-pip python3-dev pybind11-dev \
        libibverbs-dev librdmacm-dev libibumad-dev rdma-core ibverbs-providers \
        libnuma-dev libhwloc-dev libaio-dev liburing-dev \
        libcpprest-dev libssl-dev zlib1g-dev uuid-dev libgflags-dev libgtest-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages meson tomlkit pyYAML

# Everything is installed twice: into / for the next build steps, and into /stage for the runtime image.
RUN mkdir -p /stage/usr/local

RUN set -eux; \
    if [ "${BUILD_JOBS}" = 0 ]; then echo "$(nproc)" > /jobs; else echo "${BUILD_JOBS}" > /jobs; fi; \
    echo "jobs: $(cat /jobs)"

# GDRCopy userspace library (UCX and libfabric use it for small GPU copies)
RUN set -eux; \
    curl -fsSL --retry 3 -o /tmp/gdrcopy.tar.gz "${GITHUB_URL}/NVIDIA/gdrcopy/archive/refs/tags/${GDRCOPY_VERSION}.tar.gz"; \
    tar xzf /tmp/gdrcopy.tar.gz -C /tmp; \
    cd /tmp/gdrcopy-*; \
    make -j"$(cat /jobs)" lib; \
    make lib_install prefix=/usr/local; \
    make lib_install DESTDIR=/stage prefix=/usr/local; \
    ldconfig; \
    rm -rf /tmp/gdrcopy*

# UCX with CUDA, verbs, rdmacm and GDRCopy; --enable-mt for NIXL progress threads
RUN set -eux; \
    git clone --depth 1 -b "${UCX_VERSION}" "${GITHUB_URL}/openucx/ucx.git" /tmp/ucx; \
    cd /tmp/ucx; \
    ./autogen.sh; \
    ./contrib/configure-release --prefix=/usr/local --with-cuda=/usr/local/cuda --with-verbs --with-rdmacm \
        --with-gdrcopy=/usr/local --enable-mt --without-go; \
    make -j"$(cat /jobs)"; \
    make install; \
    make install DESTDIR=/stage; \
    ldconfig; \
    rm -rf /tmp/ucx

# libfabric for the NIXL LIBFABRIC plugin: verbs (InfiniBand / RoCE) and EFA (AWS) providers
RUN set -eux; \
    case ",${NIXL_PLUGINS}," in *,LIBFABRIC,*) ;; *) echo "LIBFABRIC not in NIXL_PLUGINS, skipped"; exit 0 ;; esac; \
    curl -fsSL --retry 3 -o /tmp/libfabric.tar.bz2 \
        "${GITHUB_URL}/ofiwg/libfabric/releases/download/${LIBFABRIC_VERSION}/libfabric-${LIBFABRIC_VERSION#v}.tar.bz2"; \
    tar xjf /tmp/libfabric.tar.bz2 -C /tmp; \
    cd /tmp/libfabric-*; \
    ./configure --prefix=/usr/local --enable-verbs --enable-efa \
        --disable-psm3 --disable-opx --disable-usnic --disable-rstream \
        --with-cuda=/usr/local/cuda --enable-cuda-dlopen --with-gdrcopy=/usr/local --enable-gdrcopy-dlopen; \
    make -j"$(cat /jobs)"; \
    make install; \
    make install DESTDIR=/stage; \
    ldconfig; \
    rm -rf /tmp/libfabric*

# Abseil and gRPC from source: NIXL needs absl/log, which the Ubuntu 24.04 Abseil does not have,
# and gRPC (for etcd-cpp-api) must be built against the same Abseil.
RUN set -eux; \
    git clone --depth 1 -b "${ABSL_TAG}" "${GITHUB_URL}/abseil/abseil-cpp.git" /tmp/abseil-cpp; \
    cmake -S /tmp/abseil-cpp -B /tmp/abseil-cpp/build -G Ninja -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=20 -DBUILD_SHARED_LIBS=ON \
        -DABSL_PROPAGATE_CXX_STD=ON -DABSL_ENABLE_INSTALL=ON -DABSL_BUILD_TESTING=OFF; \
    cmake --build /tmp/abseil-cpp/build -j"$(cat /jobs)"; \
    cmake --install /tmp/abseil-cpp/build; \
    DESTDIR=/stage cmake --install /tmp/abseil-cpp/build; \
    ldconfig; \
    rm -rf /tmp/abseil-cpp

# gRPC: only the submodules it builds as modules (c-ares, protobuf, re2); Abseil, OpenSSL and zlib
# come from packages, so the rest of its ~20 submodules is not cloned.
RUN set -eux; \
    if [ "${GITHUB_URL}" != https://github.com ]; then \
        git config --global url."${GITHUB_URL}/".insteadOf https://github.com/; \
    fi; \
    git clone --depth 1 -b "${GRPC_TAG}" "${GITHUB_URL}/grpc/grpc.git" /tmp/grpc; \
    git -C /tmp/grpc submodule update --init --depth 1 third_party/cares/cares third_party/protobuf third_party/re2; \
    cmake -S /tmp/grpc -B /tmp/grpc/cmake/build -G Ninja -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_PREFIX_PATH=/usr/local -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=20 -DBUILD_SHARED_LIBS=ON \
        -DgRPC_INSTALL=ON -DgRPC_BUILD_TESTS=OFF -Dabsl_DIR=/usr/local/lib/cmake/absl \
        -DgRPC_SSL_PROVIDER=package -DgRPC_ABSL_PROVIDER=package -DgRPC_PROTOBUF_PROVIDER=module \
        -DgRPC_ZLIB_PROVIDER=package \
        -DgRPC_BUILD_CSHARP_EXT=OFF -DgRPC_BUILD_GRPC_CSHARP_PLUGIN=OFF -DgRPC_BUILD_GRPC_NODE_PLUGIN=OFF \
        -DgRPC_BUILD_GRPC_OBJECTIVE_C_PLUGIN=OFF -DgRPC_BUILD_GRPC_PHP_PLUGIN=OFF \
        -DgRPC_BUILD_GRPC_PYTHON_PLUGIN=OFF -DgRPC_BUILD_GRPC_RUBY_PLUGIN=OFF; \
    cmake --build /tmp/grpc/cmake/build -j"$(cat /jobs)"; \
    cmake --install /tmp/grpc/cmake/build; \
    DESTDIR=/stage cmake --install /tmp/grpc/cmake/build; \
    ldconfig; \
    rm -rf /tmp/grpc

# etcd-cpp-api: NIXLBench ranks meet through etcd
RUN set -eux; \
    git init /tmp/etcd-cpp-apiv3; \
    cd /tmp/etcd-cpp-apiv3; \
    git remote add origin "${GITHUB_URL}/etcd-cpp-apiv3/etcd-cpp-apiv3.git"; \
    git fetch --depth 1 origin "${ETCD_CPP_REF}"; \
    git checkout --detach FETCH_HEAD; \
    sed -i '/^find_dependency(cpprestsdk)$/d' etcd-cpp-api-config.in.cmake; \
    cmake -S . -B build -G Ninja -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_PREFIX_PATH=/usr/local \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_ETCD_CORE_ONLY=ON -DBUILD_ETCD_TESTS=OFF \
        -DETCD_CMAKE_CXX_STANDARD=20 -DCMAKE_CXX_STANDARD=20; \
    cmake --build build -j"$(cat /jobs)"; \
    cmake --install build; \
    DESTDIR=/stage cmake --install build; \
    ldconfig; \
    rm -rf /tmp/etcd-cpp-apiv3

# NIXL and NIXLBench
RUN set -eux; \
    git clone --depth 1 -b "${NIXL_REF}" "${NIXL_REPO}" /tmp/nixl \
        || { git init /tmp/nixl && cd /tmp/nixl && git remote add origin "${NIXL_REPO}" \
             && git fetch --depth 1 origin "${NIXL_REF}" && git checkout --detach FETCH_HEAD; }; \
    cd /tmp/nixl; \
    echo "$(git rev-parse HEAD)" > /stage/nixl-commit; \
    meson setup build --prefix=/usr/local/nixl --buildtype=release \
        -Denable_plugins="${NIXL_PLUGINS}" -Dnixl_cuda_arch_list="${NIXL_CUDA_ARCH}" \
        -Ducx_path=/usr/local -Dlibfabric_path=/usr/local \
        -Dbuild_tests=false -Dbuild_examples=false -Dinstall_headers=true; \
    ninja -C build -j"$(cat /jobs)"; \
    ninja -C build install; \
    DESTDIR=/stage ninja -C build install; \
    arch="$(uname -m)"; \
    printf '%s\n' "/usr/local/nixl/lib/${arch}-linux-gnu" "/usr/local/nixl/lib/${arch}-linux-gnu/plugins" \
        > /etc/ld.so.conf.d/nixl.conf; \
    ldconfig; \
    cd /tmp/nixl/benchmark/nixlbench; \
    meson setup build -Dnixl_path=/usr/local/nixl/ -Dprefix=/usr/local/nixlbench --buildtype=release; \
    ninja -C build -j"$(cat /jobs)"; \
    DESTDIR=/stage ninja -C build install; \
    rm -rf /tmp/nixl

#========== runtime ==============
FROM ${BASE_RUNTIME_IMAGE}

ARG BASE_BUILD_IMAGE
ARG BASE_RUNTIME_IMAGE
ARG NIXL_REPO=https://github.com/ai-dynamo/nixl.git
ARG NIXL_REF=v1.5.2
ARG NIXL_PLUGINS=UCX,LIBFABRIC,GDS,GDS_MT,POSIX
ARG NIXL_CUDA_ARCH=auto
ARG UCX_VERSION=v1.20.0
ARG LIBFABRIC_VERSION=v1.21.0
ARG GDRCOPY_VERSION=v2.5.1
ARG ABSL_TAG=lts_2025_08_14
ARG GRPC_TAG=v1.73.0
ARG ETCD_CPP_REF=7c6e714f188f9576e25e0350cac4181139eec23e
ARG NIXL_PROFILE=""
ARG GIT_COMMIT_VERSION=""

ENV DEBIAN_FRONTEND=noninteractive

# etcd: rank rendezvous; openssh: mtest mpi mode and testNixl multi-host runs.
# libcufile for the GDS plugins comes from the CUDA apt repository of the base image.
RUN set -eux; \
    cuda_ver="$(echo "${CUDA_VERSION}" | cut -d. -f1,2 | tr . -)"; \
    extra=""; \
    case ",${NIXL_PLUGINS}," in *,GDS,*|*,GDS_MT,*) extra="libcufile-${cuda_ver}" ;; esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        rdma-core ibverbs-providers ibverbs-utils infiniband-diags libibverbs1 librdmacm1t64 libibumad3 \
        libnuma1 libhwloc15 libaio1t64 liburing2 libcpprest2.10 libssl3t64 zlib1g libuuid1 libgflags2.2 libgomp1 \
        etcd-server etcd-client openssh-server openssh-client \
        iproute2 iputils-ping procps jq curl ${extra}; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /stage/usr/local/ /usr/local/
COPY --from=build /stage/nixl-commit /tmp/nixl-commit
COPY tools/ /usr/local/bin/

ENV PATH=/usr/local/nixlbench/bin:/usr/local/nixl/bin:${PATH} \
    LD_LIBRARY_PATH=/usr/local/nixlbench/lib:/usr/local/lib:${LD_LIBRARY_PATH} \
    PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# /usr/local/nixl/lib/plugins links to the arch dependent plugin directory. Every plugin of
# NIXL_PLUGINS must exist, and no library may be missing except the driver ones (libcuda,
# libnvidia-ml), which the NVIDIA container toolkit injects at run time.
RUN set -eux; \
    arch="$(uname -m)"; \
    printf '%s\n' "/usr/local/nixl/lib/${arch}-linux-gnu" "/usr/local/nixl/lib/${arch}-linux-gnu/plugins" \
        > /etc/ld.so.conf.d/nixl.conf; \
    ln -s "/usr/local/nixl/lib/${arch}-linux-gnu/plugins" /usr/local/nixl/lib/plugins; \
    ldconfig; \
    chmod +x /usr/local/bin/run-nixlbench /usr/local/bin/testNixl; \
    for p in $(echo "${NIXL_PLUGINS}" | tr ',' ' '); do \
        test -e "/usr/local/nixl/lib/plugins/libplugin_${p}.so" || { echo "plugin ${p} missing" >&2; exit 1; }; \
    done; \
    missing="$(for f in /usr/local/nixlbench/bin/nixlbench /usr/local/nixl/lib/plugins/*.so; do ldd "$f"; done \
        | grep 'not found' | grep -vE 'libcuda\.so|libnvidia-ml\.so' | sort -u || true)"; \
    [ -z "${missing}" ] || { echo "missing libraries:" >&2; echo "${missing}" >&2; exit 1; }; \
    mkdir -p /run/sshd; \
    printf '%s\n' \
        "profile=${NIXL_PROFILE}" \
        "git_commit=${GIT_COMMIT_VERSION}" \
        "base_build_image=${BASE_BUILD_IMAGE}" \
        "base_runtime_image=${BASE_RUNTIME_IMAGE}" \
        "cuda_version=${CUDA_VERSION}" \
        "arch=${arch}" \
        "nixl_repo=${NIXL_REPO}" \
        "nixl_ref=${NIXL_REF}" \
        "nixl_commit=$(cat /tmp/nixl-commit)" \
        "nixl_plugins=${NIXL_PLUGINS}" \
        "nixl_cuda_arch=${NIXL_CUDA_ARCH}" \
        "ucx_version=${UCX_VERSION}" \
        "libfabric_version=${LIBFABRIC_VERSION}" \
        "gdrcopy_version=${GDRCOPY_VERSION}" \
        "absl_tag=${ABSL_TAG}" \
        "grpc_tag=${GRPC_TAG}" \
        "etcd_cpp_ref=${ETCD_CPP_REF}" \
        > /etc/nixl-benchmark-build.info; \
    rm -f /tmp/nixl-commit

WORKDIR /
CMD ["/bin/bash"]
