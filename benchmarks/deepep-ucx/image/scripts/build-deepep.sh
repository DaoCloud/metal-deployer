#!/bin/bash

set -x
set -o xtrace
set -o errexit
set -o pipefail
set -o nounset

if [ -z "${ENV_BASEIMAGE_CUDA_VERISON:-}" ] ; then
  exit 0
fi

CUDA_SHORT=$(echo "${ENV_BASEIMAGE_CUDA_VERISON}" | cut -d. -f1,2)
CU_VER=$(echo "${CUDA_SHORT}" | tr -d .)

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_CACHE_DIR=1

PIP_RETRIES=${PIP_RETRIES:-10}
PIP_TIMEOUT=${PIP_TIMEOUT:-600}

python3 -m pip install --upgrade pip --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}"
TORCH_WHL_INDEX_URL=${TORCH_WHL_INDEX_URL:-"https://download.pytorch.org/whl/cu${CU_VER}"}
pip3 install torch numpy packaging --extra-index-url "${TORCH_WHL_INDEX_URL}" --retries "${PIP_RETRIES}" --timeout "${PIP_TIMEOUT}" --no-cache-dir

NVSHMEM_VERSION_RAW=${ENV_NVSHMEM_VERSION:-"v3.4.5-0"}
NVSHMEM_VERSION=${NVSHMEM_VERSION_RAW#v}
NVSHMEM_VERSION=${NVSHMEM_VERSION%-*}

apt-get update
apt-get install -y --no-install-recommends xz-utils

HOST_ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
if [ "${HOST_ARCH}" != "amd64" ] && [ "${HOST_ARCH}" != "x86_64" ] ; then
  echo "unsupported architecture for DeepEP install: ${HOST_ARCH}" >&2
  exit 1
fi

rm -rf /opt/nvshmem || true
mkdir -p /opt/nvshmem

case "${HOST_ARCH}" in
  amd64|x86_64)
    NVSHMEM_URL_ARCH_DIR=x64
    NVSHMEM_URL_ARCH_TAG=x86_64
    ;;
  *)
    echo "unsupported architecture for NVSHMEM download: ${HOST_ARCH}" >&2
    exit 1
    ;;
esac
NVSHMEM_TARBALL_URL="https://developer.download.nvidia.cn/compute/redist/nvshmem/${NVSHMEM_VERSION}/builds/cuda12/txz/agnostic/${NVSHMEM_URL_ARCH_DIR}/libnvshmem-linux-${NVSHMEM_URL_ARCH_TAG}-${NVSHMEM_VERSION}_cuda12-archive.tar.xz"
echo "downloading NVSHMEM for ${HOST_ARCH}: ${NVSHMEM_TARBALL_URL}"
curl -fsSL -o /tmp/nvshmem.tar.xz "${NVSHMEM_TARBALL_URL}"

rm -rf /tmp/nvshmem-extract || true
mkdir -p /tmp/nvshmem-extract
tar -xf /tmp/nvshmem.tar.xz -C /tmp/nvshmem-extract
rm -f /tmp/nvshmem.tar.xz

NVSHMEM_EXTRACT_ROOT=$(find /tmp/nvshmem-extract -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ -z "${NVSHMEM_EXTRACT_ROOT}" ] ; then
  echo "Failed to extract NVSHMEM tarball"
  exit 1
fi

cp -a "${NVSHMEM_EXTRACT_ROOT}/"* /opt/nvshmem/
rm -rf /tmp/nvshmem-extract

if [ ! -f /opt/nvshmem/lib/libnvshmem_host.so ] ; then
  echo "NVSHMEM install did not produce /opt/nvshmem/lib/libnvshmem_host.so"
  exit 1
fi

rm -rf /buildDeepEP /buildDeepGEMM || true
mkdir -p /buildDeepEP /buildDeepGEMM

rm -rf /tmp/deepep || true
mkdir -p /tmp/deepep
cd /tmp/deepep
curl -fsSL -o DeepEP.zip https://${ENV_GITHUB_ARTIFACTORY}/deepseek-ai/DeepEP/archive/${ENV_DEEPEP_VERSION}.zip
unzip DeepEP.zip
rm -rf /opt/DeepEP || true
DEEPEP_EXTRACT_DIR=$(find /tmp/deepep -mindepth 1 -maxdepth 1 -type d -name 'DeepEP-*' | head -n 1)
if [ -z "${DEEPEP_EXTRACT_DIR}" ] ; then
  echo "Failed to find extracted DeepEP source directory under /tmp/deepep"
  exit 1
fi
mv "${DEEPEP_EXTRACT_DIR}" /opt/DeepEP

if ls /buildGdrcopy/libgdrapi_*.deb >/dev/null 2>&1 ; then
  dpkg -i /buildGdrcopy/libgdrapi_*.deb
fi

rm -rf /tmp/deepep
cd /opt/DeepEP
sed -i 's/#define NUM_CPU_TIMEOUT_SECS 100/#define NUM_CPU_TIMEOUT_SECS 1000/' csrc/kernels/configs.cuh
export TORCH_CUDA_ARCH_LIST="${ENV_TORCH_CUDA_ARCH_LIST}"
export NVSHMEM_DIR=/opt/nvshmem
export CFLAGS="${CFLAGS:-} -fcommon"
export CXXFLAGS="${CXXFLAGS:-} -fcommon"
python setup.py build
DEEPEP_SO_PATH=$(find build -maxdepth 2 -type f -name 'deep_ep_cpp*.so' | head -n 1)
if [ -z "${DEEPEP_SO_PATH}" ] ; then
  echo "Failed to find built deep_ep_cpp shared object under /opt/DeepEP/build"
  exit 1
fi
DEEPEP_SO_BASENAME=$(basename "${DEEPEP_SO_PATH}")
if [ ! -e "/opt/DeepEP/${DEEPEP_SO_BASENAME}" ] ; then
  ln -sf "${DEEPEP_SO_PATH}" "/opt/DeepEP/${DEEPEP_SO_BASENAME}"
fi
ln -sf "${DEEPEP_SO_PATH}" /opt/DeepEP/deep_ep_cpp.so
TORCH_LIB_DIR=$(python3 - <<'PY'
import pathlib
import torch
print(pathlib.Path(torch.__file__).resolve().parent / 'lib')
PY
)
if [ -d "${TORCH_LIB_DIR}" ] ; then
  export LD_LIBRARY_PATH="${TORCH_LIB_DIR}:${LD_LIBRARY_PATH:-}"
fi
python3 - <<'PY'
import importlib
import sys
sys.path.insert(0, '/opt/DeepEP')
mod = importlib.import_module('deep_ep_cpp')
print(f"validated source-tree import: {getattr(mod, '__file__', '')}")
PY

MAX_JOBS=${ENV_BUILD_AND_DOWNLOAD_PARALLEL} pip wheel --no-build-isolation . -w /buildDeepEP
cd /
rm -rf /tmp/deepep

rm -rf /opt/DeepGEMM || true
git clone -b "${ENV_DEEPGEMM_VERSION}" --depth 1 https://${ENV_GITHUB_ARTIFACTORY}/deepseek-ai/DeepGEMM.git /opt/DeepGEMM
cd /opt/DeepGEMM
git submodule update --init --recursive
python setup.py bdist_wheel
cp dist/*.whl /buildDeepGEMM/
cd /