#!/bin/bash
set -e

# ==============================================================================
# Ubuntu Server ISO Remaster Tool (Final Stable)
# Integrated: Serial Log (console=ttyS0), Dual Boot (BIOS+UEFI), Auto EFI Gen
# ==============================================================================

# 1. Permission Check
if [ "$(id -u)" -ne 0 ]; then echo "❌ Must run as root (sudo)"; exit 1; fi

BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CI_WORK_DIR="${CI_WORK_DIR:-${BASE_DIR}/.ci-work}"
WORK_DIR="${BUILD_WORK_DIR:-${CI_WORK_DIR}/build_workspace}"
ISO_SRC_DIR="${ISO_DIR:-${CI_WORK_DIR}/iso}"
PKG_DIR="${PACKAGE_DIR:-${CI_WORK_DIR}/packages}"
SCRIPT_DIR="${BASE_DIR}/scripts"
CONFIG_DIR="${CONFIG_DIR:-${BASE_DIR}/config}"
USER_DATA_FILE="${BASE_DIR}/config/cloud-init/user-data"
MANIFEST_FILE="${MANIFEST_FILE:-${BASE_DIR}/manifest.yaml}"
RESOLVE_MANIFEST="${BASE_DIR}/tools/resolve_manifest.rb"
RENDER_USER_DATA="${BASE_DIR}/tools/render_user_data.rb"

# Generate effective manifest (merges basic + CUDA profile)
EFFECTIVE_MANIFEST="${WORK_DIR}/effective-manifest.yaml"
resolve_manifest() {
    local profile="${1:-${CUDA_PROFILE:-}}"
    if [ -f "$RESOLVE_MANIFEST" ] && [ -f "$MANIFEST_FILE" ]; then
        ruby "$RESOLVE_MANIFEST" "$MANIFEST_FILE" "$profile" yaml > "$EFFECTIVE_MANIFEST"
        echo "$EFFECTIVE_MANIFEST"
    else
        echo "$MANIFEST_FILE"
    fi
}
OUTPUT_ISO="${OUTPUT_ISO:-${CI_WORK_DIR}/custom-ubuntu.iso}"

# 2. Dependency Check
REQUIRED_TOOLS="xorriso sed find mtools mkfs.vfat ruby"
for tool in $REQUIRED_TOOLS; do
    if ! command -v $tool &> /dev/null; then
        echo "🔧 Installing dependency: $tool"
        if [ "$tool" == "mkfs.vfat" ]; then apt-get update -qq && apt-get install -y -qq dosfstools
        else apt-get update -qq && apt-get install -y -qq $tool; fi
    fi
done

# 3. Find ISO
INPUT_ISO=$(find "$ISO_SRC_DIR" -maxdepth 1 -name "*.iso" | head -n 1)
[ -z "$INPUT_ISO" ] && { echo "❌ No ISO found in iso/ directory"; exit 1; }

echo "🚀 Starting Build: $(basename "$INPUT_ISO")"

# 4. Clean & Extract
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/iso-content"
mkdir -p "$(dirname "$OUTPUT_ISO")"
echo "📂 Extracting ISO..."
xorriso -osirrox on -indev "$INPUT_ISO" -extract / "$WORK_DIR/iso-content" 2>/dev/null
chmod -R u+w "$WORK_DIR/iso-content"

# 5. Inject Resources
echo "💉 Injecting resources..."
TARGET_RES_DIR="$WORK_DIR/iso-content/resource"
mkdir -p "$TARGET_RES_DIR/packages"
[ -d "$PKG_DIR" ] && cp -r "$PKG_DIR/." "$TARGET_RES_DIR/packages/"
[ -d "$SCRIPT_DIR" ] && { cp -r "$SCRIPT_DIR" "$TARGET_RES_DIR/scripts"; chmod -R +x "$TARGET_RES_DIR/scripts"; }
[ -d "$CONFIG_DIR" ] && cp -r "$CONFIG_DIR" "$TARGET_RES_DIR/config"
mkdir -p "$TARGET_RES_DIR/config"

# Generate effective manifest for current CUDA profile
MANIFEST_EFFECTIVE=$(resolve_manifest)

if [ -f "$MANIFEST_EFFECTIVE" ]; then
    ruby -ryaml -rshellwords -e '
      env = YAML.safe_load_file(ARGV.fetch(0), aliases: true).fetch("install_environment", {}) || {}
      env.each do |key, value|
        next unless key.match?(/\A[A-Z0-9_]+\z/)
        puts "#{key}=#{value.to_s.shellescape}"
      end
    ' "$MANIFEST_EFFECTIVE" > "$TARGET_RES_DIR/config/install.env"
fi
if [ -n "${NVIDIA_DRIVER_BRANCH:-}" ]; then
    printf 'NVIDIA_DRIVER_BRANCH=%q\n' "$NVIDIA_DRIVER_BRANCH" >> "$TARGET_RES_DIR/config/install.env"
fi

if [ -f "$USER_DATA_FILE" ]; then
    mkdir -p "$WORK_DIR/iso-content/nocloud"
    if [ -f "$MANIFEST_EFFECTIVE" ] && [ -f "$RENDER_USER_DATA" ]; then
        ruby "$RENDER_USER_DATA" "$USER_DATA_FILE" "$MANIFEST_EFFECTIVE" "$WORK_DIR/iso-content/nocloud/user-data"
    else
        cp "$USER_DATA_FILE" "$WORK_DIR/iso-content/nocloud/user-data"
    fi
    touch "$WORK_DIR/iso-content/nocloud/meta-data"
    touch "$WORK_DIR/iso-content/nocloud/vendor-data"
else
    echo "❌ Missing cloud-init user-data file: ${USER_DATA_FILE}"; exit 1
fi

# 6. Modify GRUB (With Serial Console Fix)
echo "🔧 Modifying GRUB..."
GRUB_CFG=$(find "$WORK_DIR/iso-content" -name "grub.cfg" -print -quit)
if [ -n "$GRUB_CFG" ]; then
    # Added console=ttyS0 for QEMU logging
    CMD_INJECT='autoinstall ds=nocloud\\;s=/cdrom/nocloud/ console=ttyS0,115200n8 console=tty0'
    sed -i "s|/casper/vmlinuz|/casper/vmlinuz $CMD_INJECT|g" "$GRUB_CFG"
    sed -i 's/set timeout=[0-9]*/set timeout=1/g' "$GRUB_CFG"
else
    echo "❌ grub.cfg not found"; exit 1
fi

# 7. Prepare EFI (Auto-Generate)
echo "⚡ Preparing UEFI Boot..."
NEW_EFI_IMG="$WORK_DIR/iso-content/boot/grub/efi.img"
EFI_BOOT_IMAGE="boot/grub/efi.img"
if [ ! -f "$NEW_EFI_IMG" ]; then
    mkdir -p "$(dirname "$NEW_EFI_IMG")"
    dd if=/dev/zero of="$NEW_EFI_IMG" bs=1M count=4 status=none
    mkfs.vfat "$NEW_EFI_IMG" > /dev/null
    EFI_SRC=$(find "$WORK_DIR/iso-content" -type d -name "EFI" -o -name "efi" | head -n 1)
    [ -z "$EFI_SRC" ] && { echo "❌ EFI directory not found"; exit 1; }
    mcopy -s -i "$NEW_EFI_IMG" "$EFI_SRC" ::/
fi

# 8. Prepare BIOS (Find eltorito) 
echo "⚡ Preparing BIOS Boot..."
BIOS_BOOT_IMG=$(cd "$WORK_DIR/iso-content" && find . -name "eltorito.img" -print -quit | sed 's|^\./||')
[ -z "$BIOS_BOOT_IMG" ] && { echo "❌ eltorito.img not found"; exit 1; }

# 9. Extract MBR
echo "💾 Extracting MBR..."
MBR_TEMPLATE="$WORK_DIR/isohdpfx.bin"
dd if="$INPUT_ISO" bs=1 count=446 of="$MBR_TEMPLATE" status=none 2>/dev/null

# 10. Repack (Hybrid BIOS + UEFI)
echo "📦 Generating Hybrid ISO..."
cd "$WORK_DIR/iso-content"
set +e
xorriso -as mkisofs -r \
  -V "CUSTOM_SERVER" \
  -J -joliet-long -l \
  -iso-level 3 \
  -partition_offset 16 \
  --grub2-mbr "$MBR_TEMPLATE" \
  --mbr-force-bootable \
  -b "$BIOS_BOOT_IMG" \
  -no-emul-boot \
  -boot-load-size 4 \
  -boot-info-table \
  --grub2-boot-info \
  -eltorito-alt-boot \
  -e "$EFI_BOOT_IMAGE" \
  -no-emul-boot \
  -isohybrid-gpt-basdat \
  -o "$OUTPUT_ISO" \
  . 2>&1 | grep -v "UPDATE"
EXIT_CODE=$?
set -e

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Success: $OUTPUT_ISO"
else
    echo "❌ Build Failed"; exit 1
fi
