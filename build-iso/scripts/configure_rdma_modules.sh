#!/bin/bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: configure_rdma_modules.sh must be run as root. Try: sudo $0" >&2
    exit 1
fi

CONFIG_FILE="${RDMA_MODULES_CONFIG:-/opt/resource/config/rdma_modules.conf}"
TARGET_FILE="${RDMA_MODULES_TARGET:-/etc/rdma/modules/rdma.conf}"

default_modules=(
    ib_core
    ib_uverbs
    ib_umad
    ib_ipoib
    rdma_cm
    iw_cm
    mlx5_core
    mlx5_ib
)

mkdir -p "$(dirname "$TARGET_FILE")"

if [ -f "$CONFIG_FILE" ]; then
    sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' "$CONFIG_FILE" > "$TARGET_FILE"
elif [ -n "${RDMA_MODULES:-}" ]; then
    tr ', ' '\n' <<< "$RDMA_MODULES" | sed '/^[[:space:]]*$/d' > "$TARGET_FILE"
else
    printf '%s\n' "${default_modules[@]}" > "$TARGET_FILE"
fi

chmod 0644 "$TARGET_FILE"

while IFS= read -r module; do
    [ -n "$module" ] || continue
    modprobe "$module" || true
done < "$TARGET_FILE"

echo "Configured RDMA modules in ${TARGET_FILE}:"
cat "$TARGET_FILE"
