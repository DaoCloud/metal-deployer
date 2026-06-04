#!/bin/bash
set -euo pipefail

cat >/etc/modprobe.d/disable-nouveau.conf <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
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

if systemctl list-unit-files | grep -q '^nvidia-fabricmanager'; then
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
