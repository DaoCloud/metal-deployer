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

# DOCA package names differ across releases. Follow NVIDIA host install path.
read -r -a doca_packages <<< "${DOCA_PACKAGES:-doca-networking}"

if ! apt-get install -y --allow-downgrades "${doca_packages[@]}"; then
    apt-get install -y --allow-downgrades doca-all || \
    apt-get install -y --allow-downgrades doca-runtime doca-tools || true
fi

modprobe mlx5_core || true
modprobe mlx5_ib || true

systemctl restart openibd 2>/dev/null || true
systemctl restart rdma 2>/dev/null || true

ibv_devinfo || true
rdma link || true
