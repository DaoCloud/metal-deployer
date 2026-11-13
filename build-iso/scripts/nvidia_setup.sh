#!/bin/bash
set -euo pipefail

RESOURCE_DIR="${RESOURCE_DIR:-/opt/resource}"
IMEX_CONFIG_SRC_DIR="${IMEX_CONFIG_SRC_DIR:-${RESOURCE_DIR}/config/imex}"
INSTALL_ENV_FILE="${INSTALL_ENV_FILE:-${RESOURCE_DIR}/config/install.env}"

if [ -f "$INSTALL_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$INSTALL_ENV_FILE"
fi

has_imex() {
    command -v nvidia-imex >/dev/null 2>&1
}

# NVL72 racks: NVSwitch trays run NMX-C (GFM/NVLSM) on the switch side, so the
# node-side fabric manager must not be enabled. HGX boards (x86 manifest) keep it.
skip_fabric_manager() {
    case "${SKIP_FABRIC_MANAGER:-}" in
        1|true|yes) return 0 ;;
        0|false|no) return 1 ;;
    esac
    [ "${PLATFORM:-}" = "gb300" ] || [ "${PLATFORM:-}" = "gb200" ]
}

prepare_imex() {
    has_imex || return 0

    # Create /dev/nvidia-caps-imex-channels/channel0 at module load; CUDA needs it
    # to import fabric handles. Harmless when IMEX is not running.
    cat >/etc/modprobe.d/nvidia-imex.conf <<'EOF'
options nvidia NVreg_CreateImexChannel0=1
EOF

    if [ -f "${IMEX_CONFIG_SRC_DIR}/10-wait-config.conf" ]; then
        install -d -m 0755 /etc/systemd/system/nvidia-imex.service.d
        install -m 0644 "${IMEX_CONFIG_SRC_DIR}/10-wait-config.conf" \
            /etc/systemd/system/nvidia-imex.service.d/10-wait-config.conf
    fi
    if [ -f "${IMEX_CONFIG_SRC_DIR}/nodes_config.cfg.example" ]; then
        install -d -m 0755 /etc/nvidia-imex
        install -m 0644 "${IMEX_CONFIG_SRC_DIR}/nodes_config.cfg.example" \
            /etc/nvidia-imex/nodes_config.cfg.example
    fi
    systemctl daemon-reload || true

    # Enabled now, started later: the drop-in's ConditionPathExists keeps the
    # unit a no-op until imex-configure.sh writes nodes_config.cfg.
    systemctl enable nvidia-imex.service 2>/dev/null || true
    if [ -f /etc/nvidia-imex/nodes_config.cfg ]; then
        systemctl restart nvidia-imex.service || true
    fi
}

cat >/etc/modprobe.d/disable-nouveau.conf <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
prepare_imex
update-initramfs -u || true

if lsmod | grep -q '^nouveau'; then
    for dev in /sys/bus/pci/drivers/nouveau/*:*:*.*; do
        [ -e "$dev" ] || continue
        echo "$(basename "$dev")" > /sys/bus/pci/drivers/nouveau/unbind || true
    done
    modprobe -r nouveau || true
fi
modprobe nvidia || true

if [ ! -e /usr/local/cuda ]; then
    cuda_dir="$(find /usr/local -maxdepth 1 -type d -name 'cuda-*' | sort -V | tail -n 1)"
    if [ -n "$cuda_dir" ]; then
        ln -sfn "$cuda_dir" /usr/local/cuda
    fi
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi -pm 1 || true
    nvidia-smi topo -m || true
fi

systemctl enable nvidia-persistenced.service 2>/dev/null || true
systemctl restart nvidia-persistenced.service 2>/dev/null || true

if skip_fabric_manager; then
    echo "[nvidia_setup] Multi-node NVLink platform: leaving nvidia-fabricmanager disabled (fabric is managed by NMX-C on the switch tray)."
    systemctl disable --now nvidia-fabricmanager.service 2>/dev/null || true
elif systemctl list-unit-files | grep -q '^nvidia-fabricmanager'; then
    systemctl enable nvidia-fabricmanager.service || true
    systemctl restart nvidia-fabricmanager.service || true
fi

if systemctl list-unit-files | grep -q '^nvidia-dcgm'; then
    systemctl enable nvidia-dcgm.service || true
    systemctl restart nvidia-dcgm.service || true
elif systemctl list-unit-files | grep -q '^dcgm'; then
    systemctl enable dcgm.service || true
    systemctl restart dcgm.service || true
fi

cat >/etc/profile.d/cuda.sh <<'EOF'
export CUDA_HOME=/usr/local/cuda
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
EOF

if [ -d /usr/local/cuda/bin ]; then
    ln -sfn /usr/local/cuda/bin/nvcc /usr/local/bin/nvcc
fi
