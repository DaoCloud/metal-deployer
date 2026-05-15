#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: setup_container_runtime.sh must be run as root. Try: sudo $0" >&2
    exit 1
fi

PACKAGE_DIR="${PACKAGE_DIR:-/opt/resource/packages}"

install_local_container_debs() {
    [ -d "$PACKAGE_DIR" ] || return 0
    shopt -s nullglob

    local debs=(
        "$PACKAGE_DIR"/containerd.io*.deb
        "$PACKAGE_DIR"/docker-ce-cli*.deb
        "$PACKAGE_DIR"/docker-ce-[0-9]*.deb
        "$PACKAGE_DIR"/docker-buildx-plugin*.deb
        "$PACKAGE_DIR"/docker-compose-plugin*.deb
        "$PACKAGE_DIR"/libnvidia-container1*.deb
        "$PACKAGE_DIR"/libnvidia-container-tools*.deb
        "$PACKAGE_DIR"/nvidia-container-toolkit-base*.deb
        "$PACKAGE_DIR"/nvidia-container-toolkit-[0-9]*.deb
    )

    if [ "${#debs[@]}" -gt 0 ]; then
        apt-get install -y --allow-downgrades --no-install-recommends "${debs[@]}"
    fi
}

install_local_container_debs

if ! command -v docker >/dev/null 2>&1 || ! command -v containerd >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

mkdir -p /etc/docker
if command -v nvidia-ctk >/dev/null 2>&1; then
    nvidia-ctk runtime configure --runtime=docker
    nvidia-ctk runtime configure --runtime=containerd || true
else
    cat >/etc/docker/daemon.json <<'EOF'
{
  "exec-opts": ["native.cgroupdriver=systemd"],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  }
}
EOF
fi

systemctl daemon-reload
systemctl enable containerd docker
systemctl restart containerd
systemctl restart docker

docker version || true
docker info | sed -n '/Runtimes/,+3p' || true
