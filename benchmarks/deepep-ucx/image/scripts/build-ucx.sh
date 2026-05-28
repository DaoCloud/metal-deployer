#!/bin/bash

set -x
set -o xtrace
set -o errexit
set -o pipefail
set -o nounset

ENV_GITHUB_ARTIFACTORY=${ENV_GITHUB_ARTIFACTORY:-"github.com"}

rm -rf /buildUcxRootfs || true
mkdir -p /buildUcxRootfs/usr/local

if [ -z "${ENV_BASEIMAGE_CUDA_VERISON:-}" ] ; then
  exit 0
fi

rm -rf /tmp/ucx || true
git clone -b ${ENV_UCX_VERSION} https://${ENV_GITHUB_ARTIFACTORY}/openucx/ucx.git /tmp/ucx
cd /tmp/ucx
./autogen.sh
./configure --prefix=/usr/local --with-cuda=/usr/local/cuda --with-verbs --with-rdmacm --enable-optimizations
make -j${ENV_BUILD_AND_DOWNLOAD_PARALLEL}
make install DESTDIR=/buildUcxRootfs
cd /
rm -rf /tmp/ucx