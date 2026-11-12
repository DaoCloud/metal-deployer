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
MANIFEST_FILE="${MANIFEST_FILE:-${BASE_DIR}/manifests/manifest.yaml}"
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
REQUIRED_TOOLS="xorriso sed find mtools mkfs.vfat ruby dpkg-scanpackages"
for tool in $REQUIRED_TOOLS; do
    if ! command -v $tool &> /dev/null; then
        echo "🔧 Installing dependency: $tool"
        if [ "$tool" == "mkfs.vfat" ]; then apt-get update -qq && apt-get install -y -qq dosfstools
        elif [ "$tool" == "dpkg-scanpackages" ]; then apt-get update -qq && apt-get install -y -qq dpkg-dev
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
find "$TARGET_RES_DIR/packages" -type f -name '*.tmp' -delete
if find "$TARGET_RES_DIR/packages" -maxdepth 1 -name '*.deb' -print -quit | grep -q .; then
    (
        cd "$TARGET_RES_DIR/packages"
        dpkg-scanpackages . /dev/null > Packages
        gzip -9c Packages > Packages.gz
    )
fi

# Generate effective manifest for current CUDA profile
MANIFEST_EFFECTIVE=$(resolve_manifest)

# Target platform: arm64 ISOs (GB300) are UEFI-only and use a different serial port.
TARGET_ARCH=""
SERIAL_CONSOLE=""
if [ -f "$MANIFEST_EFFECTIVE" ]; then
    TARGET_ARCH=$(ruby -ryaml -e 'm = YAML.safe_load_file(ARGV.fetch(0), aliases: true) || {}; puts((m.dig("platform", "arch") || m.dig("install_environment", "TARGET_ARCH")).to_s)' "$MANIFEST_EFFECTIVE")
    SERIAL_CONSOLE=$(ruby -ryaml -e 'm = YAML.safe_load_file(ARGV.fetch(0), aliases: true) || {}; puts((m.dig("install_environment", "SERIAL_CONSOLE") || m.dig("platform", "serial_console")).to_s)' "$MANIFEST_EFFECTIVE")
fi
SERIAL_CONSOLE="${SERIAL_CONSOLE:-ttyS0}"
PLATFORM_NAME=""
if [ -f "$MANIFEST_EFFECTIVE" ]; then
    PLATFORM_NAME=$(ruby -ryaml -e 'm = YAML.safe_load_file(ARGV.fetch(0), aliases: true) || {}; puts((m.dig("platform", "name") || m.dig("install_environment", "PLATFORM")).to_s)' "$MANIFEST_EFFECTIVE")
fi
# Volume ID lets ISO consumers (NICo artifact tooling, BMC media lists) tell
# platforms apart; x86 keeps the historical label. Override with ISO_VOLUME_ID.
if [ -z "${ISO_VOLUME_ID:-}" ]; then
    if [ -n "$PLATFORM_NAME" ]; then
        ISO_VOLUME_ID=$(echo "METAL-${PLATFORM_NAME}-${TARGET_ARCH:-x86_64}" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9_\n-' '_' | cut -c1-32)
    else
        ISO_VOLUME_ID="CUSTOM_SERVER"
    fi
fi
UEFI_ONLY=false
[ "$TARGET_ARCH" = "arm64" ] && UEFI_ONLY=true
echo "🎯 Target arch: ${TARGET_ARCH:-default} (UEFI-only: ${UEFI_ONLY}, serial: ${SERIAL_CONSOLE}, volume: ${ISO_VOLUME_ID})"

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
for env_key in MIRROR_SOURCE INTRANET_MIRROR; do
    if [ -n "${!env_key:-}" ]; then
        printf '%s=%q\n' "$env_key" "${!env_key}" >> "$TARGET_RES_DIR/config/install.env"
    fi
done

if [ -f "$USER_DATA_FILE" ]; then
    mkdir -p "$WORK_DIR/iso-content/nocloud"
    if [ -f "$MANIFEST_EFFECTIVE" ] && [ -f "$RENDER_USER_DATA" ]; then
        ruby "$RENDER_USER_DATA" "$USER_DATA_FILE" "$MANIFEST_EFFECTIVE" "$WORK_DIR/iso-content/nocloud/user-data"
    else
        cp "$USER_DATA_FILE" "$WORK_DIR/iso-content/nocloud/user-data"
    fi
    touch "$WORK_DIR/iso-content/nocloud/meta-data"
    touch "$WORK_DIR/iso-content/nocloud/vendor-data"
    # Same config at the ISO root: when the installer is network-booted
    # (NICo: kernel/initrd via iPXE, url=<ISO>, ds=nocloud-net) the ISO's GRUB
    # and /nocloud are bypassed. Subiquity falls back to /cdrom/autoinstall.yaml
    # when the served cloud-config has no autoinstall section.
    cp "$WORK_DIR/iso-content/nocloud/user-data" "$WORK_DIR/iso-content/autoinstall.yaml"
else
    echo "❌ Missing cloud-init user-data file: ${USER_DATA_FILE}"; exit 1
fi

# Machine-readable description for tooling that unpacks the ISO (e.g. the
# NICo artifact publisher): where kernel/initrd/autoinstall live and how to boot it.
mkdir -p "$WORK_DIR/iso-content/.metal-deployer"
ISO_VERSION="${ISO_VERSION:-$(git -C "$BASE_DIR" describe --tags --always --dirty 2>/dev/null || echo unknown)}"
# init_system.sh reports it in /var/lib/metal-deployer/state.json.
printf 'ISO_VERSION=%q\n' "$ISO_VERSION" >> "$TARGET_RES_DIR/config/install.env"
ISO_VERSION="$ISO_VERSION" ISO_VOLUME_ID="$ISO_VOLUME_ID" \
    ruby -ryaml -rjson -rtime - "$MANIFEST_EFFECTIVE" "$WORK_DIR/iso-content" <<'RUBY' > "$WORK_DIR/iso-content/.metal-deployer/release.json"
manifest_path, root = ARGV
m = File.file?(manifest_path) ? (YAML.safe_load_file(manifest_path, aliases: true) || {}) : {}
env = m["install_environment"] || {}
disk_info = File.read(File.join(root, ".disk/info")).strip rescue nil
present = ->(p) { File.exist?(File.join(root, p)) ? p : nil }
release = {
  "schema" => 1,
  "product" => "metal-deployer",
  "version" => ENV.fetch("ISO_VERSION", "unknown"),
  "build_time" => Time.now.utc.iso8601,
  "volume_id" => ENV.fetch("ISO_VOLUME_ID", ""),
  "platform" => m.dig("platform", "name") || env["PLATFORM"],
  "arch" => m.dig("platform", "arch") || env["TARGET_ARCH"] || "amd64",
  "base_os" => disk_info,
  "cuda_profile" => ENV["CUDA_PROFILE"] || env["CUDA_PROFILE"],
  "target_kernel" => { "package" => env["KERNEL_PACKAGE"], "release" => env["KERNEL_RELEASE"] }.compact,
  "nvidia_driver_branch" => ENV["NVIDIA_DRIVER_BRANCH"] || env["NVIDIA_DRIVER_BRANCH"],
  "serial_console" => env["SERIAL_CONSOLE"] || m.dig("platform", "serial_console"),
  "boot" => {
    "kernel" => present.("casper/vmlinuz"),
    "initrd" => present.("casper/initrd"),
    "autoinstall" => present.("autoinstall.yaml"),
    "nocloud" => present.("nocloud/user-data"),
  }.compact,
  "network_boot" => {
    "nico_ipxe_template" => "ubuntu-autoinstall",
    "artifacts" => { "install_iso" => "<this ISO>", "kernel" => "casper/vmlinuz", "initrd" => "casper/initrd" },
    "kernel_params" => "ip=dhcp url=<ISO URL> autoinstall ds=nocloud-net;s=<seed URL>",
    "user_data" => "optional: autoinstall.yaml is used when the served cloud-config has no autoinstall section; supply nocloud/user-data as userData when NICo phone-home is enabled",
    "note" => "url= downloads the whole ISO into RAM; the node needs more free RAM than the ISO size",
  },
}
puts JSON.pretty_generate(release)
RUBY

# 6. Modify GRUB (With Serial Console Fix)
echo "🔧 Modifying GRUB..."
GRUB_CFG="$WORK_DIR/iso-content/boot/grub/grub.cfg"
[ -f "$GRUB_CFG" ] || GRUB_CFG=$(find "$WORK_DIR/iso-content" -name "grub.cfg" -print -quit)
if [ -n "$GRUB_CFG" ]; then
    # Serial console for QEMU / BMC SOL logging (ttyS0 on x86, ttyAMA0 on Grace)
    CMD_INJECT="autoinstall ds=nocloud\\\\;s=/cdrom/nocloud/ console=${SERIAL_CONSOLE},115200n8 console=tty0"
    sed -i "s|/casper/vmlinuz|/casper/vmlinuz $CMD_INJECT|g" "$GRUB_CFG"
    sed -i 's/set timeout=[0-9]*/set timeout=1/g' "$GRUB_CFG"
else
    echo "❌ grub.cfg not found"; exit 1
fi

if [ "$UEFI_ONLY" = "true" ]; then
    # arm64 Ubuntu ISOs have no El Torito BIOS image; the ESP is an appended
    # GPT partition. Replay the original ISO's boot setup (ESP read straight
    # from the source ISO) instead of hand-building BIOS+UEFI options.
    echo "📦 Generating UEFI-only ISO (replaying original boot layout)..."
    # Absolute path: the replayed --interval options reference the source ISO
    # by the path given here, and xorriso runs from iso-content below.
    # --modification-date is kept on purpose: it defines the ISO9660 UUID.
    BOOT_OPTS=$(xorriso -indev "$(realpath "$INPUT_ISO")" -report_el_torito as_mkisofs 2>/dev/null | grep -v '^-V ')
    [ -n "$BOOT_OPTS" ] || { echo "❌ Cannot read boot layout from ${INPUT_ISO}"; exit 1; }
    echo "$BOOT_OPTS" > "$WORK_DIR/boot-options.txt"
    eval "set -- $(tr '\n' ' ' <<< "$BOOT_OPTS")"
    cd "$WORK_DIR/iso-content"
    set +e
    xorriso -as mkisofs -r \
      -V "$ISO_VOLUME_ID" \
      -J -joliet-long -l \
      -iso-level 3 \
      "$@" \
      -o "$OUTPUT_ISO" \
      . 2>&1 | grep -v "UPDATE"
    EXIT_CODE=${PIPESTATUS[0]}
    set -e
    echo ""
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "✅ Success: $OUTPUT_ISO"
        exit 0
    fi
    echo "❌ Build Failed"; exit 1
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
  -V "$ISO_VOLUME_ID" \
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
EXIT_CODE=${PIPESTATUS[0]}
set -e

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Success: $OUTPUT_ISO"
else
    echo "❌ Build Failed"; exit 1
fi
