#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"

register_local_repo_keys() {
    find /var -maxdepth 3 -type f -name '*keyring.gpg' -path '*doca*' \
        -exec cp -f {} /usr/share/keyrings/ \; || true
}

shopt -s nullglob
doca_repo_debs=("$PACKAGE_DIR"/doca-host_*-ubuntu2404_*.deb "$PACKAGE_DIR"/doca*-repo-ubuntu2404*.deb "$PACKAGE_DIR"/doca-sdk-repo-ubuntu2404*.deb)

if [ "${#doca_repo_debs[@]}" -gt 0 ]; then
    dpkg -i "${doca_repo_debs[@]}" || apt-get -f install -y
    register_local_repo_keys
fi

apt-get update

# Install the OFED kernel stack and RDMA userspace directly instead of the
# broad doca-networking meta package. doca-networking pulls
# python3-doca-openvswitch -> python3-prometheus-client/python3-sortedcontainers,
# which are Ubuntu universe packages unavailable offline; that breaks the whole
# apt transaction and silently skips mlnx-ofed-kernel-dkms. The kernel DKMS
# package provides the PeerDirect-enabled ib_core required by nvidia_peermem
# (GPUDirect RDMA). --no-install-recommends keeps the closure offline-installable.
read -r -a doca_packages <<< "${DOCA_PACKAGES:-mlnx-ofed-kernel-dkms mlnx-ofed-kernel-utils ibverbs-providers libibverbs1 ibverbs-utils librdmacm1 infiniband-diags}"

apt-get install -y --no-install-recommends --allow-downgrades "${doca_packages[@]}"

modprobe mlx5_core || true
modprobe mlx5_ib || true

systemctl restart openibd 2>/dev/null || true
systemctl restart rdma 2>/dev/null || true

ibv_devinfo || true
rdma link || true
