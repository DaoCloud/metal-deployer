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
GPU_DRIVER_DEBS=()
GPU_LIBRARY_DEBS=()
DCGM_DEBS=()

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$SUMMARY_LOG"
}

# Machine-readable progress for the control plane, which polls it over LOM SSH.
# STAGE_PLAN must list every run_stage/skip_stage name in main, in order.
STATE_DIR="${STATE_DIR:-/var/lib/metal-deployer}"
STATE_FILE="${STATE_FILE:-${STATE_DIR}/state.json}"
STAGE_PLAN=(
    ssh_keys prepare apt_sources base_packages nouveau offline_debs
    doca_install gpu_debs rdma_modules container_runtime docker_images
    cpu_performance nvidia_setup hpcx_install gdrcopy_build test_tools_build gpu_burn
)
declare -A STAGE_STATUS=() STAGE_RC=() STAGE_START=() STAGE_END=() STAGE_REASON=()
OVERALL_STATUS="running"
OVERALL_RC=""
CURRENT_STAGE=""
STARTED_AT=""
FINISHED_AT=""

now_utc() {
    TZ=UTC0 printf '%(%Y-%m-%dT%H:%M:%SZ)T' -1
}

# json_val <str|opt|num> <value>: print a JSON value; "opt" and "num" print
# null when empty. No command substitution, to keep each state write cheap.
json_val() {
    local kind="$1" s="$2"
    if [ -z "$s" ] && [ "$kind" != "str" ]; then
        printf 'null'
    elif [ "$kind" = "num" ]; then
        printf '%s' "$s"
    else
        s="${s//\\/\\\\}"
        s="${s//\"/\\\"}"
        s="${s//$'\n'/\\n}"
        s="${s//$'\r'/}"
        s="${s//$'\t'/\\t}"
        printf '"%s"' "$s"
    fi
}

# json_field <key> <kind> <value> [separator]
json_field() {
    printf '"%s": ' "$1"
    json_val "$2" "$3"
    printf '%s' "${4:-, }"
}

write_state() {
    local tmp="${STATE_FILE}.tmp.$$" name sep="" boot_id="" now
    [ -r /proc/sys/kernel/random/boot_id ] && read -r boot_id < /proc/sys/kernel/random/boot_id
    now="$(now_utc)"

    {
        printf '{\n  '
        json_field schema num 1 $',\n  '
        json_field iso_version str "${ISO_VERSION:-unknown}" $',\n  '
        json_field hostname str "${HOSTNAME:-}" $',\n  '
        json_field boot_id opt "$boot_id" $',\n  '
        json_field overall str "$OVERALL_STATUS" $',\n  '
        json_field exit_code num "$OVERALL_RC" $',\n  '
        json_field current_stage opt "$CURRENT_STAGE" $',\n  '
        json_field started_at opt "$STARTED_AT" $',\n  '
        json_field updated_at str "$now" $',\n  '
        json_field finished_at opt "$FINISHED_AT" $',\n  '
        printf '"stages": [\n'
        for name in "${STAGE_PLAN[@]}"; do
            printf '%s    {' "$sep"
            json_field name str "$name"
            json_field status str "${STAGE_STATUS[$name]:-pending}"
            json_field rc num "${STAGE_RC[$name]:-}"
            json_field started_at opt "${STAGE_START[$name]:-}"
            json_field finished_at opt "${STAGE_END[$name]:-}"
            json_field reason opt "${STAGE_REASON[$name]:-}"
            json_field log str "${LOG_DIR}/${name}.log" '}'
            sep=$',\n'
        done
        printf '\n  ]\n}\n'
    } > "$tmp" && mv -f "$tmp" "$STATE_FILE"
}

# Progress reporting must never break the install.
save_state() {
    write_state 2>/dev/null || rm -f "${STATE_FILE}.tmp.$$" 2>/dev/null || true
}

state_init() {
    mkdir -p "$STATE_DIR" || true
    STARTED_AT="$(now_utc)"
    save_state
}

# state_set <stage> <running|success|failed|skipped> [rc] [reason]
state_set() {
    local name="$1" status="$2" rc="${3:-}" reason="${4:-}"

    STAGE_STATUS[$name]="$status"
    STAGE_RC[$name]="$rc"
    STAGE_REASON[$name]="$reason"
    if [ "$status" = "running" ]; then
        STAGE_START[$name]="$(now_utc)"
        STAGE_END[$name]=""
        CURRENT_STAGE="$name"
    else
        STAGE_END[$name]="$(now_utc)"
        CURRENT_STAGE=""
    fi
    save_state
}

state_finish() {
    local status="$1" rc="$2" name

    for name in "${STAGE_PLAN[@]}"; do
        if [ "${STAGE_STATUS[$name]:-}" = "running" ]; then
            STAGE_STATUS[$name]="failed"
            STAGE_RC[$name]="$rc"
            STAGE_END[$name]="$(now_utc)"
        fi
    done
    OVERALL_STATUS="$status"
    OVERALL_RC="$rc"
    CURRENT_STAGE=""
    FINISHED_AT="$(now_utc)"
    save_state
}

# Catches failures outside run_stage and interrupted runs.
on_exit() {
    local rc=$?
    if [ "$OVERALL_STATUS" = "running" ]; then
        [ "$rc" -ne 0 ] || rc=1
        state_finish failed "$rc"
    fi
}

run_stage() {
    local name="$1"
    shift

    state_set "$name" running
    log "========== START ${name} =========="
    if "$@" >> "${LOG_DIR}/${name}.log" 2>&1; then
        state_set "$name" success 0
        log "========== DONE  ${name} =========="
    else
        local rc=$?
        state_set "$name" failed "$rc"
        log "========== FAIL  ${name} rc=${rc}; see ${LOG_DIR}/${name}.log =========="
        return "$rc"
    fi
}

skip_stage() {
    local name="$1" reason="$2"
    state_set "$name" skipped "" "$reason"
    log "Skip ${name}: ${reason}"
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
        libpci-dev libudev-dev libtool libtool-bin devscripts debhelper fakeroot check openmpi-bin libopenmpi-dev

    # RDMA userspace tools are installed best-effort and intentionally kept out
    # of the atomic install above. The inbox rdma-core/infiniband-diags debs pull
    # libibmad5/libibnetdisc5t64, which are not part of the offline set, so a
    # single atomic transaction would fail and take the essential build toolchain
    # (libtool/autoconf/automake) down with it. The proper OFED versions of these
    # tools are installed later by the doca_install stage.
    apt-get install -y --no-install-recommends \
        rdma-core ibverbs-utils infiniband-diags || \
        log "Skip inbox RDMA tools in base_packages (unmet offline deps); provided later by doca_install."

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

    # Only packages that are actually installed: dpkg-query -W also lists
    # packages dpkg merely knows about (state "un"), and holding those blocks
    # the offline install of e.g. linux-tools-<platform kernel>.
    dpkg-query -W -f='${db:Status-Status} ${Package}\n' 'linux-image-*' 'linux-headers-*' 'linux-modules-*' 'linux-tools-*' 2>/dev/null \
        | awk '$1 == "installed" {print $2}' \
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

# The CUDA local repo (1:3.3.0) and DOCA repo (3.2.2) ship newer dkms than the
# Ubuntu archive. Mixing versions breaks module bookkeeping: dkms 3.3 records
# builds under "aarch64" while 3.0 used "arm64", so kernel postinst autoinstall
# fails with "already installed". Keep the offline Ubuntu dkms.
pin_offline_dkms() {
    local deb ver=""
    for deb in "$PACKAGE_DIR"/dkms_*.deb; do
        [ -f "$deb" ] || continue
        ver="$(dpkg-deb -f "$deb" Version 2>/dev/null || true)"
    done
    [ -n "$ver" ] || return 0
    cat >/etc/apt/preferences.d/metal-deployer-dkms <<EOF
# metal-deployer: keep Ubuntu dkms; CUDA/DOCA repos ship incompatible newer versions.
Package: dkms
Pin: version ${ver}
Pin-Priority: 1001
EOF
}

install_offline_debs() {
    [ -d "$PACKAGE_DIR" ] || return 0

    shopt -s nullglob

    local repo_debs=()
    local direct_debs=()
    local skipped_debs=()
    local deb pkg src ver
    local -A newest_installed=()
    GPU_DRIVER_DEBS=()
    GPU_LIBRARY_DEBS=()
    DCGM_DEBS=()
    # Re-runs (e.g. after a failed first boot): later stages may already have
    # upgraded a source package past the offline set (DOCA replaces the inbox
    # rdma-core libraries). Installing the older debs then conflicts, so skip
    # debs whose source package is installed at a newer version. A fresh first
    # boot has nothing newer than the build-time solution, so nothing is skipped.
    # Compare source versions: binary versions of one source can differ (e.g.
    # util-linux binaries with and without an epoch).
    while read -r st src ver; do
        [ "$st" = "installed" ] || continue
        if [ -z "${newest_installed[$src]:-}" ] || dpkg --compare-versions "$ver" gt "${newest_installed[$src]}"; then
            newest_installed[$src]="$ver"
        fi
    done < <(dpkg-query -W -f='${db:Status-Status} ${source:Package} ${source:Version}\n' 2>/dev/null)

    for deb in "$PACKAGE_DIR"/*.deb; do
        pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null || basename "$deb")"
        case "$pkg" in
            cuda-repo-*|*-repo-ubuntu*)
                repo_debs+=("$deb")
                ;;
            cuda-drivers|nvidia-driver*|nvidia-fabricmanager*|nvidia-persistenced)
                GPU_DRIVER_DEBS+=("$deb")
                ;;
            libnccl*)
                GPU_LIBRARY_DEBS+=("$deb")
                ;;
            datacenter-gpu-manager*|dcgmi|libdcgm|nv-hostengine)
                DCGM_DEBS+=("$deb")
                ;;
            *)
                src="$(dpkg-deb -f "$deb" Source 2>/dev/null)"
                ver="$(dpkg-deb -f "$deb" Version 2>/dev/null)"
                # "Source: name (version)" when it differs from the binary version.
                if [[ "$src" == *"("*")"* ]]; then
                    ver="${src#*(}"
                    ver="${ver%)*}"
                fi
                src="${src%% *}"
                [ -n "$src" ] || src="$pkg"
                if [ -n "${newest_installed[$src]:-}" ] && [ -n "$ver" ] &&
                    dpkg --compare-versions "${newest_installed[$src]}" gt "$ver"; then
                    skipped_debs+=("${pkg}=${ver} (${src} ${newest_installed[$src]} installed)")
                    continue
                fi
                direct_debs+=("$deb")
                ;;
        esac
    done
    if [ "${#skipped_debs[@]}" -gt 0 ]; then
        log "offline_debs: skip ${#skipped_debs[@]} deb(s) superseded by newer installed versions: ${skipped_debs[*]}"
    fi

    # run_stage calls stages inside "if", where set -e is ignored, so every
    # step returns explicitly; otherwise a failed transaction is logged DONE.
    if [ "${#repo_debs[@]}" -gt 0 ]; then
        dpkg -i "${repo_debs[@]}" || apt-get -f install -y || return
        find /var -maxdepth 3 -type f -name '*keyring.gpg' -path '*repo*' \
            -exec cp -f {} /usr/share/keyrings/ \; || true
        apt-get update || return
    fi

    # The offline set is solved at build time and may upgrade packages that
    # disable_automatic_kernel_updates holds (linux-tools-common, platform
    # kernel tools); holds only guard against unattended upgrades.
    if [ "${#direct_debs[@]}" -gt 0 ]; then
        apt-get install -y --allow-downgrades --allow-change-held-packages "${direct_debs[@]}" || return
    fi

    # CUDA toolkit is software-only and can be validated in a VM; driver/DCGM/NCCL
    # packages are GPU-dependent and are installed only when hardware is present.
    apt-get install -y --allow-downgrades --allow-change-held-packages "$CUDA_TOOLKIT_PACKAGE" || return
}

# Driver components shipped inside the CUDA local repo (e.g. 580.126.20 in the
# 13.0.3 repo) must never be selected on platforms that use a different driver.
cuda_local_repo_driver_versions() {
    local deb pkg ver
    for deb in "$PACKAGE_DIR"/*.deb; do
        [ -f "$deb" ] || continue
        pkg="$(dpkg-deb -f "$deb" Package 2>/dev/null || true)"
        case "$pkg" in
            cuda-repo-*-local) ;;
            *) continue ;;
        esac
        # 13.0.3-580.126.20-1 bundles driver 580.126.20; toolkit-only repos
        # such as 13.4.3-1 bundle none.
        ver="$(dpkg-deb -f "$deb" Version 2>/dev/null || true)"
        case "$ver" in
            *-*-*) ver="${ver#*-}"; ver="${ver%-*}" ;;
            *) continue ;;
        esac
        [ -n "$ver" ] && echo "$ver"
    done | sort -u
}

# GB300 / Grace path: Canonical-signed R580 server-open driver, prebuilt for the
# platform kernel that install_target_kernel.sh put in place during autoinstall.
install_signed_server_open_driver() {
    local running driver_ver blocked
    running="$(uname -r)"
    if [ -n "${KERNEL_RELEASE:-}" ] && [ "$running" != "$KERNEL_RELEASE" ]; then
        log "ERROR: running kernel ${running}, expected ${KERNEL_RELEASE}. The signed NVIDIA modules only match ${KERNEL_RELEASE}; check /var/log/metal-deployer-target-kernel.log and GRUB default."
        return 1
    fi
    if [ -z "${NVIDIA_DRIVER_PACKAGES:-}" ]; then
        log "ERROR: NVIDIA_DRIVER_PACKAGES is empty for NVIDIA_DRIVER_BRANCH=${NVIDIA_DRIVER_BRANCH}."
        return 1
    fi

    # apt rejects "Package: *" with a version pin, so name every package of
    # the registered CUDA local repo that carries the bundled driver version.
    {
        echo "# metal-deployer: keep CUDA local repo drivers out; ${NVIDIA_DRIVER_BRANCH} is used instead."
        for driver_ver in $(cuda_local_repo_driver_versions); do
            blocked=$(awk -v v="$driver_ver" '/^Package:/{p=$2} /^Version:/{sub(/^[0-9]+:/, "", $2); if (index($2, v) == 1) print p}' \
                /var/cuda-repo-*/Packages 2>/dev/null | sort -u | tr '\n' ' ')
            [ -n "$blocked" ] || continue
            echo "Package: ${blocked}"
            echo "Pin: version ${driver_ver}*"
            echo "Pin-Priority: -1"
            echo
        done
    } > /etc/apt/preferences.d/metal-deployer-nvidia-driver
    apt-get update

    log "Installing ${NVIDIA_DRIVER_BRANCH} driver ${NVIDIA_DRIVER_VERSION:-} for ${running}: ${NVIDIA_DRIVER_PACKAGES}"
    # shellcheck disable=SC2086
    apt-get install -y --allow-downgrades --allow-change-held-packages --no-install-recommends ${NVIDIA_DRIVER_PACKAGES} || return
    modprobe nvidia || log "WARNING: modprobe nvidia failed; see dmesg."
}

install_gpu_debs() {
    if has_nvidia_gpu; then
        if [[ "$NVIDIA_DRIVER_BRANCH" == *-server-open-signed ]]; then
            install_signed_server_open_driver
        elif [ "$NVIDIA_DRIVER_BRANCH" = "580-server" ]; then
            log "NVIDIA_DRIVER_BRANCH=580-server; installing Ubuntu R580 server driver."
            apt-get install -y --allow-downgrades \
                nvidia-driver-580-server \
                nvidia-utils-580-server
        elif [ "$NVIDIA_DRIVER_BRANCH" = "default" ]; then
            if [ "${#GPU_DRIVER_DEBS[@]}" -gt 0 ]; then
                apt-get install -y --allow-downgrades "${GPU_DRIVER_DEBS[@]}"
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

        if [ "${#GPU_LIBRARY_DEBS[@]}" -gt 0 ]; then
            apt-get install -y --allow-downgrades "${GPU_LIBRARY_DEBS[@]}"
        fi
        apt-get install -y --allow-downgrades libnccl2 libnccl-dev

        if [ "${#DCGM_DEBS[@]}" -gt 0 ]; then
            apt-get install -y --allow-downgrades "${DCGM_DEBS[@]}" || log "Skip DCGM local deb install: dependency resolution failed."
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

install_yq() {
    local bin="${PACKAGE_DIR}/yq_linux_$(dpkg --print-architecture)"
    if [ ! -f "$bin" ]; then
        log "Skip yq install: ${bin} not found."
        return 0
    fi
    install -m 0755 "$bin" /usr/local/bin/yq
    /usr/local/bin/yq --version
}

run_optional_script() {
    local script="$1"
    shift || true
    [ -x "${SCRIPT_DIR}/${script}" ] || return 0
    "${SCRIPT_DIR}/${script}" "$@"
}

has_nvidia_gpu() {
    # PCI vendor 10de with display class 0300 (VGA) or 0302 (3D controller)
    # matches every NVIDIA GPU, including ones newer than the local pci.ids
    # (GB300 may show as "Device xxxx"). Grace bridges (0604) are not matched.
    lspci -n -d 10de: 2>/dev/null | grep -qE ' 030[02]: ' ||
        lspci | grep -qiE 'nvidia.*(3d|vga|display)|tesla|h100|h200|a100|a800|l40|l4'
}

has_rdma_adapter() {
    lspci | grep -qiE 'mellanox|nvidia.*connectx|bluefield'
}

prepare_system() {
    disable_automatic_kernel_updates || return
    pin_offline_dkms
}

disable_nouveau() {
    configure_nouveau_blacklist || return
    release_nouveau_devices
}

main() {
    state_init
    trap on_exit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    log "Metal deployer first boot initialization started."

    # First, so the control plane can SSH in and poll state.json during the long stages.
    run_stage ssh_keys run_optional_script configure_ssh.sh
    run_stage prepare prepare_system
    run_stage apt_sources run_optional_script configure-apt-sources.sh
    disable_automatic_kernel_updates
    run_stage base_packages install_base_packages
    disable_automatic_kernel_updates
    run_stage nouveau disable_nouveau
    run_stage offline_debs install_offline_debs
    run_stage yq_install install_yq

    run_stage doca_install run_optional_script install_doca.sh
    run_stage gpu_debs install_gpu_debs
    run_stage rdma_modules run_optional_script configure_rdma_modules.sh

    run_stage container_runtime run_optional_script setup_container_runtime.sh
    run_stage docker_images run_optional_script load_docker_images.sh
    run_stage cpu_performance install_cpu_performance_service
    run_stage nvidia_setup run_optional_script nvidia_setup.sh

    run_stage hpcx_install run_optional_script install_hpcx.sh

    if has_nvidia_gpu || has_rdma_adapter; then
        run_stage gdrcopy_build run_optional_script install_gdrcopy.sh
        run_stage test_tools_build run_optional_script install_test_tools.sh
    else
        skip_stage gdrcopy_build "no NVIDIA GPU or RDMA adapter detected"
        skip_stage test_tools_build "no NVIDIA GPU or RDMA adapter detected"
    fi

    if ! has_nvidia_gpu; then
        skip_stage gpu_burn "no NVIDIA GPU detected"
    elif [ "${ENABLE_GPU_BURN_BUILD:-true}" != "true" ]; then
        skip_stage gpu_burn "ENABLE_GPU_BURN_BUILD=${ENABLE_GPU_BURN_BUILD}"
    else
        run_stage gpu_burn run_optional_script install_gpu_burn.sh
    fi

    touch /root/installed
    state_finish success 0
    log "Metal deployer first boot initialization finished."
}

main "$@"
