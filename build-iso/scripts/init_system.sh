#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: init_system.sh must be run as root. Try: sudo $0" >&2
    exit 1
fi

RESOURCE_DIR="${RESOURCE_DIR:-/opt/resource}"
PACKAGE_DIR="${PACKAGE_DIR:-${RESOURCE_DIR}/packages}"
SCRIPT_DIR="${SCRIPT_DIR:-${RESOURCE_DIR}/scripts}"
LOG_DIR="${LOG_DIR:-/var/log/metal-deployer}"
SUMMARY_LOG="${SUMMARY_LOG:-${LOG_DIR}/install-summary.log}"
INSTALL_ENV_FILE="${INSTALL_ENV_FILE:-${RESOURCE_DIR}/config/install.env}"

export DEBIAN_FRONTEND=noninteractive

mkdir -p "$LOG_DIR"

if [ -f "$INSTALL_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$INSTALL_ENV_FILE"
fi

NVIDIA_DRIVER_BRANCH="${NVIDIA_DRIVER_BRANCH:-default}"
CUDA_TOOLKIT_PACKAGE="${CUDA_TOOLKIT_PACKAGE:-cuda-toolkit-13-2}"
NVIDIA_DRIVER_VERSION="${NVIDIA_DRIVER_VERSION:-}"

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$SUMMARY_LOG"
}

run_stage() {
    local name="$1"
    shift

    log "========== START ${name} =========="
    if "$@" >> "${LOG_DIR}/${name}.log" 2>&1; then
        log "========== DONE  ${name} =========="
    else
        local rc=$?
        log "========== FAIL  ${name} rc=${rc}; see ${LOG_DIR}/${name}.log =========="
        return "$rc"
    fi
}

install_base_packages() {
    apt-get update
    apt-get install -y --no-install-recommends \
        fping arping ipmitool net-tools iproute2 ethtool tcpdump iperf3 traceroute \
        nvme-cli fio smartmontools mdadm lvm2 stress-ng stressapptest memtester sysstat \
        dmidecode pciutils usbutils hwloc numactl bc jq lldpd \
        unzip tar xz-utils ca-certificates gnupg curl wget vim htop tmux rsync chrony \
        build-essential gcc make dkms \
        cmake git pkg-config libglvnd-dev autoconf automake \
        libpmix-dev libboost-program-options-dev libnuma1 libnuma-dev libsubunit0 \
        libpci-dev libtool devscripts debhelper fakeroot check openmpi-bin libopenmpi-dev \
        rdma-core ibverbs-utils infiniband-diags

    apt-get install -y --no-install-recommends "linux-headers-$(uname -r)" || \
        log "Skip matching kernel headers install: linux-headers-$(uname -r) is unavailable."
}

disable_automatic_kernel_updates() {
    cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::AutocleanInterval "0";
EOF

    cat >/etc/apt/apt.conf.d/51disable-kernel-auto-upgrades <<'EOF'
Unattended-Upgrade::Package-Blacklist {
        "linux-generic";
        "linux-image-*";
        "linux-headers-*";
        "linux-modules-*";
        "linux-tools-*";
        "linux-cloud-tools-*";
};
EOF

    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service 2>/dev/null || true

    apt-mark hold \
        linux-generic \
        linux-image-generic \
        linux-headers-generic \
        linux-tools-generic \
        linux-cloud-tools-generic 2>/dev/null || true

    dpkg-query -W -f='${Package}\n' 'linux-image-*' 'linux-headers-*' 'linux-modules-*' 'linux-tools-*' 2>/dev/null \
        | xargs -r apt-mark hold 2>/dev/null || true
}

pin_nvidia_driver_branch() {
    local version="$1"
    local branch="${version%%.*}"

    [ -n "$version" ] || return 0
    [ -n "$branch" ] || return 0

    cat >/etc/apt/preferences.d/99-nvidia-local-driver.pref <<EOF
Package: cuda-drivers cuda-drivers-${branch} nvidia-driver-${branch} nvidia-dkms-${branch} nvidia-kernel-common-${branch} nvidia-kernel-source-${branch} nvidia-compute-utils-${branch} nvidia-utils-${branch} nvidia-settings libnvidia-*-${branch} xserver-xorg-video-nvidia-${branch}
Pin: version ${version}-0ubuntu1
Pin-Priority: 1001

Package: nvidia-driver-* nvidia-dkms-* nvidia-kernel-common-* nvidia-kernel-source-* nvidia-compute-utils-* nvidia-utils-* libnvidia-*-* xserver-xorg-video-nvidia-*
Pin: version *
Pin-Priority: 400
EOF
}

install_offline_debs() {
    [ -d "$PACKAGE_DIR" ] || return 0

    shopt -s nullglob

    local repo_debs=()
    local direct_debs=()
    local gpu_driver_debs=()
    local gpu_library_debs=()
    local dcgm_debs=()
    local deb pkg
    for deb in "$PACKAGE_DIR"/*.deb; do
        pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null || basename "$deb")"
        case "$pkg" in
            cuda-repo-*|*-repo-ubuntu*)
                repo_debs+=("$deb")
                ;;
            cuda-drivers|nvidia-driver*|nvidia-fabricmanager*|nvidia-persistenced)
                gpu_driver_debs+=("$deb")
                ;;
            libnccl*)
                gpu_library_debs+=("$deb")
                ;;
            datacenter-gpu-manager*)
                dcgm_debs+=("$deb")
                ;;
            *)
                direct_debs+=("$deb")
                ;;
        esac
    done

    if [ "${#repo_debs[@]}" -gt 0 ]; then
        dpkg -i "${repo_debs[@]}" || apt-get -f install -y
        find /var -maxdepth 3 -type f -name '*keyring.gpg' -path '*repo*' \
            -exec cp -f {} /usr/share/keyrings/ \; || true
        apt-get update
    fi

    if [ "${#direct_debs[@]}" -gt 0 ]; then
        apt-get install -y --allow-downgrades "${direct_debs[@]}"
    fi

    # CUDA toolkit is software-only and can be validated in a VM; driver/DCGM/NCCL
    # packages are GPU-dependent and are installed only when hardware is present.
    apt-get install -y --allow-downgrades "$CUDA_TOOLKIT_PACKAGE" || true

    if lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'; then
        if [ "$NVIDIA_DRIVER_BRANCH" = "580-server" ]; then
            log "NVIDIA_DRIVER_BRANCH=580-server; installing Ubuntu R580 server driver."
            apt-get install -y --allow-downgrades \
                nvidia-driver-580-server \
                nvidia-utils-580-server
        elif [ "$NVIDIA_DRIVER_BRANCH" = "default" ]; then
            if [ "${#gpu_driver_debs[@]}" -gt 0 ]; then
                apt-get install -y --allow-downgrades "${gpu_driver_debs[@]}"
            fi

            if [ -n "$NVIDIA_DRIVER_VERSION" ]; then
                local branch="${NVIDIA_DRIVER_VERSION%%.*}"
                pin_nvidia_driver_branch "$NVIDIA_DRIVER_VERSION"
                apt-get install -y --allow-downgrades --no-install-recommends \
                    "cuda-drivers-${branch}=${NVIDIA_DRIVER_VERSION}-0ubuntu1" \
                    "nvidia-driver-${branch}=${NVIDIA_DRIVER_VERSION}-0ubuntu1"
            else
                apt-get install -y --allow-downgrades --no-install-recommends cuda-drivers
            fi

            apt-get install -y --allow-downgrades nvidia-persistenced || log "Skip nvidia-persistenced: package is unavailable."
        else
            echo "Unsupported NVIDIA_DRIVER_BRANCH: ${NVIDIA_DRIVER_BRANCH}" >&2
            return 1
        fi

        if [ "${#gpu_library_debs[@]}" -gt 0 ]; then
            apt-get install -y --allow-downgrades "${gpu_library_debs[@]}"
        fi
        apt-get install -y --allow-downgrades libnccl2 libnccl-dev

        if [ "${#dcgm_debs[@]}" -gt 0 ]; then
            apt-get install -y --allow-downgrades "${dcgm_debs[@]}" || log "Skip DCGM local deb install: dependency resolution failed."
        fi
        apt-get install -y --allow-downgrades \
            datacenter-gpu-manager-4-core \
            datacenter-gpu-manager-4-proprietary || log "Skip DCGM package install: packages are unavailable."
    else
        log "Skip GPU-dependent deb install: no NVIDIA GPU detected."
    fi
}

configure_nouveau_blacklist() {
    cat >/etc/modprobe.d/disable-nouveau.conf <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
}

release_nouveau_devices() {
    local dev

    for dev in /sys/bus/pci/drivers/nouveau/*:*:*.*; do
        [ -e "$dev" ] || continue
        echo "$(basename "$dev")" > /sys/bus/pci/drivers/nouveau/unbind || true
    done

    modprobe -r nouveau || true
}

install_cpu_performance_service() {
    local service="${SCRIPT_DIR}/cpu-performance.service"
    [ -f "$service" ] || return 0

    install -m 0644 "$service" /etc/systemd/system/cpu-performance.service
    systemctl daemon-reload
    systemctl enable cpu-performance.service
    systemctl start cpu-performance.service || true
}

run_optional_script() {
    local script="$1"
    shift || true
    [ -x "${SCRIPT_DIR}/${script}" ] || return 0
    "${SCRIPT_DIR}/${script}" "$@"
}

has_nvidia_gpu() {
    lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'
}

has_rdma_adapter() {
    lspci | grep -qiE 'mellanox|nvidia.*connectx|bluefield'
}

main() {
    log "Metal deployer first boot initialization started."

    disable_automatic_kernel_updates
    run_stage apt_sources run_optional_script configure-apt-sources.sh
    disable_automatic_kernel_updates
    run_stage base_packages install_base_packages
    disable_automatic_kernel_updates
    configure_nouveau_blacklist
    release_nouveau_devices
    run_stage offline_debs install_offline_debs

    run_stage doca_install run_optional_script install_doca.sh
    run_stage rdma_modules run_optional_script configure_rdma_modules.sh

    run_stage ssh_keys run_optional_script configure_ssh.sh
    run_stage container_runtime run_optional_script setup_container_runtime.sh
    run_stage docker_images run_optional_script load_docker_images.sh
    run_stage cpu_performance install_cpu_performance_service
    run_stage nvidia_setup run_optional_script nvidia_setup.sh

    run_stage hpcx_install run_optional_script install_hpcx.sh

    if has_nvidia_gpu || has_rdma_adapter; then
        run_stage gdrcopy_build run_optional_script install_gdrcopy.sh
        run_stage test_tools_build run_optional_script install_test_tools.sh
    else
        log "Skip test_tools_build: no NVIDIA GPU or RDMA adapter detected."
    fi

    if has_nvidia_gpu && [ "${ENABLE_GPU_BURN_BUILD:-true}" = "true" ]; then
        run_stage gpu_burn run_optional_script install_gpu_burn.sh
    fi

    touch /root/installed
    log "Metal deployer first boot initialization finished."
}

main "$@"
