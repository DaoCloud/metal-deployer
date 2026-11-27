#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"

log() {
    echo "[install_doca] $*"
}

secure_boot_enabled() {
    local state var
    if command -v mokutil >/dev/null 2>&1; then
        state="$(mokutil --sb-state 2>/dev/null || true)"
        case "$state" in
            *"SecureBoot enabled"*) return 0 ;;
            *"SecureBoot disabled"*) return 1 ;;
        esac
    fi
    var=/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c
    [ -r "$var" ] && [ "$(od -An -tu1 -j4 -N1 "$var" 2>/dev/null | tr -d ' ')" = "1" ]
}

# dkms signs modules with this key; they load under Secure Boot only once the
# key is enrolled in MOK (needs a console confirmation in MokManager).
mok_key_enrolled() {
    local key=/var/lib/shim-signed/mok/MOK.der
    [ -f "$key" ] && command -v mokutil >/dev/null 2>&1 &&
        mokutil --test-key "$key" 2>/dev/null | grep -q 'already enrolled'
}

# DOCA's mlnx-ofed-kernel-dkms modules go to updates/dkms and take precedence
# over the inbox mlx5/ib_core. Under Secure Boot unsigned modules are rejected
# and modprobe does not fall back to the inbox ones, so every Mellanox port
# (often including the management NIC) would go down after this stage. Keep
# the inbox stack instead; GPUDirect RDMA still works through dma-buf.
# DOCA_SECURE_BOOT=install forces the install (e.g. key enrolled out of band).
if [ "${DOCA_SECURE_BOOT:-auto}" != "install" ] && secure_boot_enabled && ! mok_key_enrolled; then
    log "Secure Boot is enabled and no enrolled MOK key signs DKMS modules: skip DOCA-OFED, keep the inbox mlx5/rdma-core stack."
    log "To use DOCA: disable Secure Boot, or enroll /var/lib/shim-signed/mok/MOK.der (mokutil --import, confirm in MokManager), then rerun this stage."
    ibv_devinfo 2>/dev/null | grep -E 'hca_id|fw_ver|state' || true
    exit 0
fi

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
# package provides the OFED ib_core/mlx5 stack. On GB300 the Canonical signed
# nvidia_peermem is built against the inbox ib_core without PeerDirect symbols
# (init returns -EINVAL), so GPUDirect RDMA uses the dma-buf path
# (ib_umem_dmabuf_get). --no-install-recommends keeps the closure offline-installable.
read -r -a doca_packages <<< "${DOCA_PACKAGES:-mlnx-ofed-kernel-dkms mlnx-ofed-kernel-utils ibverbs-providers libibverbs1 ibverbs-utils librdmacm1 infiniband-diags}"

apt-get install -y --no-install-recommends --allow-downgrades "${doca_packages[@]}"

modprobe mlx5_core || true
modprobe mlx5_ib || true

systemctl restart openibd 2>/dev/null || true
systemctl restart rdma 2>/dev/null || true

ibv_devinfo || true
rdma link || true
