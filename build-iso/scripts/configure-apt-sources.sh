#!/bin/bash
#
# Copyright 2024 Authors of metal-deployer
# SPDX-License-Identifier: Apache-2.0
#
# Configuration Ubuntu 24.04 (Noble Numbat) APT MirrorSource
# Supports: aliyun, intranet mirror, or official source

set -o errexit
set -o nounset
set -o pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: configure-apt-sources.sh must be run as root. Try: sudo $0" >&2
    exit 1
fi

# Mirror source selection (can be overridden via environment variable)
MIRROR_SOURCE="${MIRROR_SOURCE:-aliyun}"  # Optional: aliyun, intranet, official

# Intranet mirror address (used when MIRROR_SOURCE=intranet)
INTRANET_MIRROR="${INTRANET_MIRROR:-http://mirrors.intranet.daocloud.io}"

echo "=========================================="
echo "Configure APT mirror source: ${MIRROR_SOURCE}"
echo "=========================================="

# Backup original sources.list
if [ -f /etc/apt/sources.list ]; then
    cp -n /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%Y%m%d%H%M%S) || true
    echo "✅ Backed up original sources.list"
fi

# Select configuration based on mirror source
case "${MIRROR_SOURCE}" in
    aliyun)
        echo "🌐 Use Aliyun mirror"
        cat <<'EOF' > /etc/apt/sources.list
# Ubuntu 24.04 (Noble Numbat) - Aliyun mirror
deb https://mirrors.aliyun.com/ubuntu/ noble main restricted universe multiverse
deb-src https://mirrors.aliyun.com/ubuntu/ noble main restricted universe multiverse

deb https://mirrors.aliyun.com/ubuntu/ noble-security main restricted universe multiverse
deb-src https://mirrors.aliyun.com/ubuntu/ noble-security main restricted universe multiverse

deb https://mirrors.aliyun.com/ubuntu/ noble-updates main restricted universe multiverse
deb-src https://mirrors.aliyun.com/ubuntu/ noble-updates main restricted universe multiverse

# deb https://mirrors.aliyun.com/ubuntu/ noble-proposed main restricted universe multiverse
# deb-src https://mirrors.aliyun.com/ubuntu/ noble-proposed main restricted universe multiverse

deb https://mirrors.aliyun.com/ubuntu/ noble-backports main restricted universe multiverse
deb-src https://mirrors.aliyun.com/ubuntu/ noble-backports main restricted universe multiverse
EOF
        ;;

    intranet)
        echo "🏢 Use intranet mirror: ${INTRANET_MIRROR}"
        cat <<EOF > /etc/apt/sources.list
# Ubuntu 24.04 (Noble Numbat) - Intranet mirror
deb ${INTRANET_MIRROR}/ubuntu/ noble main restricted universe multiverse
deb-src ${INTRANET_MIRROR}/ubuntu/ noble main restricted universe multiverse

deb ${INTRANET_MIRROR}/ubuntu/ noble-security main restricted universe multiverse
deb-src ${INTRANET_MIRROR}/ubuntu/ noble-security main restricted universe multiverse

deb ${INTRANET_MIRROR}/ubuntu/ noble-updates main restricted universe multiverse
deb-src ${INTRANET_MIRROR}/ubuntu/ noble-updates main restricted universe multiverse

deb ${INTRANET_MIRROR}/ubuntu/ noble-backports main restricted universe multiverse
deb-src ${INTRANET_MIRROR}/ubuntu/ noble-backports main restricted universe multiverse
EOF
        ;;

    official)
        echo "🌍 Use official mirror"
        cat <<'EOF' > /etc/apt/sources.list
# Ubuntu 24.04 (Noble Numbat) - Official mirror
deb http://archive.ubuntu.com/ubuntu/ noble main restricted universe multiverse
deb-src http://archive.ubuntu.com/ubuntu/ noble main restricted universe multiverse

deb http://archive.ubuntu.com/ubuntu/ noble-security main restricted universe multiverse
deb-src http://archive.ubuntu.com/ubuntu/ noble-security main restricted universe multiverse

deb http://archive.ubuntu.com/ubuntu/ noble-updates main restricted universe multiverse
deb-src http://archive.ubuntu.com/ubuntu/ noble-updates main restricted universe multiverse

deb http://archive.ubuntu.com/ubuntu/ noble-backports main restricted universe multiverse
deb-src http://archive.ubuntu.com/ubuntu/ noble-backports main restricted universe multiverse
EOF
        ;;

    *)
        echo "❌ Unknown mirror source type: ${MIRROR_SOURCE}"
        echo "Supported options: aliyun, intranet, official"
        exit 1
        ;;
esac

# Update APT cache
echo "🔄 Update APT cache..."
apt-get update

echo "=========================================="
echo "✅ APT Mirror source configuration complete"
echo "Current mirror: ${MIRROR_SOURCE}"
echo "=========================================="
