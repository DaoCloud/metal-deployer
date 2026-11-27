#!/bin/bash
# install_target_kernel.sh - install the platform kernel into the target system
# during autoinstall, so the very first boot already runs it.
#
# Called from autoinstall late-commands inside the installed system:
#   curtin in-target --target=/target -- /opt/resource/scripts/install_target_kernel.sh
#
# GB300 needs linux-nvidia-64k-hwe-24.04 (Canonical/NVIDIA requirement), and the
# Canonical-signed R580 modules are built for that kernel only. The installer
# can only install linux-generic from the ISO pool, so the platform kernel is
# installed here from the offline packages copied to /opt/resource/packages.
#
# Reads KERNEL_PACKAGE (and optional KERNEL_RELEASE) from install.env. When
# KERNEL_PACKAGE is unset (x86 manifests) this is a no-op.
set -euo pipefail

RESOURCE_DIR="${RESOURCE_DIR:-/opt/resource}"
PACKAGE_DIR="${PACKAGE_DIR:-${RESOURCE_DIR}/packages}"
INSTALL_ENV_FILE="${INSTALL_ENV_FILE:-${RESOURCE_DIR}/config/install.env}"
LOG_FILE="${LOG_FILE:-/var/log/metal-deployer-target-kernel.log}"

exec > >(tee -a "$LOG_FILE") 2>&1

log() {
    echo "[install_target_kernel] $*"
}

if [ -f "$INSTALL_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$INSTALL_ENV_FILE"
fi

if [ -z "${KERNEL_PACKAGE:-}" ]; then
    log "KERNEL_PACKAGE not set; keeping the installer kernel."
    exit 0
fi

if [ ! -f "${PACKAGE_DIR}/Packages" ] && [ ! -f "${PACKAGE_DIR}/Packages.gz" ]; then
    log "ERROR: no Packages index in ${PACKAGE_DIR}; cannot install ${KERNEL_PACKAGE}."
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

# Resolve against the offline repository only: the target has no network
# source yet, and the installer's /cdrom pool does not carry this kernel.
APT_TMP="$(mktemp -d)"
trap 'rm -rf "$APT_TMP"' EXIT
echo "deb [trusted=yes] file:${PACKAGE_DIR} ./" > "${APT_TMP}/sources.list"
mkdir -p "${APT_TMP}/sources.list.d" "${APT_TMP}/lists/partial"
APT_OPTS=(
    -o "Dir::Etc::sourcelist=${APT_TMP}/sources.list"
    -o "Dir::Etc::sourceparts=${APT_TMP}/sources.list.d"
    -o "Dir::State::lists=${APT_TMP}/lists"
    -o "APT::Get::List-Cleanup=0"
)

log "Installing ${KERNEL_PACKAGE}${KERNEL_RELEASE:+ (expect ${KERNEL_RELEASE})} from ${PACKAGE_DIR}"
apt-get "${APT_OPTS[@]}" update
# shellcheck disable=SC2086
apt-get "${APT_OPTS[@]}" install -y --no-install-recommends ${KERNEL_PACKAGE}

if [ -n "${KERNEL_RELEASE:-}" ]; then
    if [ ! -e "/boot/vmlinuz-${KERNEL_RELEASE}" ]; then
        log "ERROR: /boot/vmlinuz-${KERNEL_RELEASE} missing after installing ${KERNEL_PACKAGE}."
        exit 1
    fi
    if [ ! -e "/boot/initrd.img-${KERNEL_RELEASE}" ]; then
        log "initrd for ${KERNEL_RELEASE} missing; generating it."
        update-initramfs -c -k "${KERNEL_RELEASE}"
    fi
fi

# Keep the platform kernel pinned; init_system.sh also disables kernel auto-upgrades.
# shellcheck disable=SC2086
apt-mark hold ${KERNEL_PACKAGE} >/dev/null

# The installer kernel (user-data kernel.package: linux-generic) stays only as
# a GRUB rescue entry. Drop its metapackages and headers: with headers present
# every DKMS module (DOCA mlnx-ofed-kernel, gdrdrv) is also built for it on
# first boot, doubling build time, and the metapackages would pull newer
# generic kernels on an online upgrade. The kernel image and modules stay.
mapfile -t generic_pkgs < <(
    dpkg-query -W -f='${db:Status-Status} ${Package}\n' \
        'linux-generic*' 'linux-image-generic*' 'linux-headers-*' 'linux-tools-generic*' 2>/dev/null |
        awk '$1 == "installed" {print $2}' |
        grep -E '^linux-(generic|image-generic|tools-generic|headers-generic)|^linux-headers-[0-9.]+-[0-9]+(-generic)?$' |
        grep -v -e '64k' -e "${KERNEL_RELEASE:-${KERNEL_PACKAGE}}" || true
)
if [ "${#generic_pkgs[@]}" -gt 0 ]; then
    log "Removing installer kernel metapackages/headers: ${generic_pkgs[*]}"
    apt-get purge -y "${generic_pkgs[@]}" ||
        log "WARNING: could not remove ${generic_pkgs[*]}; DKMS will also build for the installer kernel."
fi

# GRUB boots the newest kernel by default; refresh the menu explicitly in case
# the kernel postinst hook skipped it inside the installer chroot.
if command -v update-grub >/dev/null 2>&1; then
    update-grub || log "WARNING: update-grub failed; first boot may use the installer kernel."
fi

log "Installed kernels:"
ls -1 /boot/vmlinuz-* 2>/dev/null || true
