#!/bin/bash
set -euo pipefail

BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CI_WORK_DIR="${CI_WORK_DIR:-${BASE_DIR}/.ci-work}"
MANIFEST_FILE="${MANIFEST_FILE:-${BASE_DIR}/manifests/manifest.yaml}"
RESOLVE_MANIFEST="${BASE_DIR}/tools/resolve_manifest.rb"
ISO_DIR="${ISO_DIR:-${CI_WORK_DIR}/iso}"
PACKAGE_DIR="${PACKAGE_DIR:-${CI_WORK_DIR}/packages}"
IMAGE_DIR="${IMAGE_DIR:-${PACKAGE_DIR}/images}"
CONFIG_DIR="${CONFIG_DIR:-${BASE_DIR}/config}"
BASE_ISO_PATH="${BASE_ISO_PATH:-}"
DOWNLOAD_IMAGES="${DOWNLOAD_IMAGES:-true}"
DRY_RUN="${DRY_RUN:-false}"
FORCE="${FORCE:-false}"
SSH_PUBLIC_KEY_FILE="${SSH_PUBLIC_KEY_FILE:-}"
CACHE_BASE_DIR="${CACHE_BASE_DIR:-${CACHE_DIR:-/home/runner/actions-runner/_work/metal-deployer-cache}}"
CACHE_DIR="$CACHE_BASE_DIR"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Prepare files required by build.sh:
  - base Ubuntu ISO into iso/
  - package archives into packages/
  - optional Docker image tar files into packages/images/
  - optional SSH public keys into config/ssh_authorized_keys

Options:
  --manifest PATH          Manifest path. Default: build-iso/manifests/manifest.yaml
  --skip-images            Do not pull/save Docker images
  --ssh-public-key PATH    Copy this public key to config/ssh_authorized_keys
  --force                  Re-download existing files
  --dry-run                Validate remote URLs and print actions without downloading
  -h, --help               Show this help

Environment:
  MANIFEST_FILE, ISO_DIR, PACKAGE_DIR, IMAGE_DIR, CONFIG_DIR
  BASE_ISO_PATH
  DOWNLOAD_IMAGES=true|false
  FORCE=true|false
  DRY_RUN=true|false
  CACHE_DIR or CACHE_BASE_DIR Local cache directory (e.g. /home/runner/cache)
                              Subdirs: iso/, base-packages/, cuda12/, cuda13/
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --manifest)
            MANIFEST_FILE="$2"
            shift 2
            ;;
        --skip-images)
            DOWNLOAD_IMAGES=false
            shift
            ;;
        --ssh-public-key)
            SSH_PUBLIC_KEY_FILE="$2"
            shift 2
            ;;
        --force)
            FORCE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

run() {
    if [ "$DRY_RUN" = "true" ]; then
        printf '[dry-run] '
        printf '%q ' "$@"
        printf '\n'
    else
        "$@"
    fi
}

curl_args_for_url() {
    local url="$1"
    local -n args_ref="$2"
    args_ref=(-fL --retry 5 --retry-delay 2 --retry-all-errors)

    if [[ "$url" == *://mirrors.intranet.daocloud.io/* ]]; then
        args_ref+=(--noproxy mirrors.intranet.daocloud.io)
    fi
}

validate_remote_url() {
    local url="$1"
    local curl_args=()

    curl_args_for_url "$url" curl_args

    echo "Validate URL: ${url}"
    if curl "${curl_args[@]}" -I -o /dev/null "$url"; then
        return 0
    fi

    echo "HEAD probe failed, retry with ranged GET: ${url}" >&2
    curl "${curl_args[@]}" --range 0-0 -o /dev/null "$url"
}

validate_docker_image() {
    local image="$1"

    echo "Validate Docker image: ${image}"
    docker manifest inspect "$image" >/dev/null
}

require_tool() {
    local tool="$1"
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing required tool: ${tool}" >&2
        return 1
    fi
}

# Infer cache subdirectory from package URL
cache_subdir_for_package() {
    local url="$1"
    case "$url" in
        *cuda12*|*12.8*)
            echo "cuda12"
            ;;
        *cuda13*|*13.2*)
            echo "cuda13"
            ;;
        *)
            echo "base-packages"
            ;;
    esac
}

# Try to copy from CACHE_BASE_DIR; returns 0 on cache hit
try_cache_copy() {
    local cache_path="$1"
    local output="$2"
    if [ -n "$CACHE_BASE_DIR" ] && [ -f "$cache_path" ]; then
        echo "Cache hit: ${cache_path}"
        mkdir -p "$(dirname "$output")"
        cp "$cache_path" "$output"
        return 0
    fi
    return 1
}

# Store downloaded file into CACHE_BASE_DIR
store_to_cache() {
    local cache_path="$1"
    local output="$2"
    if [ -n "$CACHE_BASE_DIR" ]; then
        mkdir -p "$(dirname "$cache_path")"
        cp "$output" "$cache_path"
        echo "Cached: ${cache_path}"
    fi
}

download_file() {
    local url="$1"
    local output="$2"
    local cache_subdir="${3:-}"
    local tmp_output="${output}.tmp"
    local curl_args=()
    local cache_path=""
    local filename
    filename=$(basename "$output")

    curl_args_for_url "$url" curl_args
    curl_args+=(--continue-at -)

    if [ "$DRY_RUN" = "true" ]; then
        validate_remote_url "$url"
        echo "[dry-run] Would download to ${output}"
        return 0
    fi

    # Check local output first
    if [ -f "$output" ] && [ "$FORCE" != "true" ]; then
        echo "Skip existing: ${output}"
        if [ -n "$cache_subdir" ]; then
            cache_path="${CACHE_BASE_DIR}/${cache_subdir}/${filename}"
            if [ -n "$CACHE_BASE_DIR" ] && [ ! -f "$cache_path" ]; then
                store_to_cache "$cache_path" "$output"
            fi
        fi
        return 0
    fi

    # Check runner-local cache
    if [ -n "$cache_subdir" ]; then
        cache_path="${CACHE_BASE_DIR}/${cache_subdir}/${filename}"
        if try_cache_copy "$cache_path" "$output"; then
            return 0
        fi
    fi

    mkdir -p "$(dirname "$output")"
    if [ "$FORCE" = "true" ]; then
        run rm -f "$output" "$tmp_output"
    fi
    echo "Download: ${url}"
    echo "      -> ${output}"
    run curl "${curl_args[@]}" -o "$tmp_output" "$url"
    run mv "$tmp_output" "$output"

    # Store to runner-local cache for future runs
    if [ -n "$cache_subdir" ]; then
        store_to_cache "$cache_path" "$output"
    fi
}

extract_manifest_json() {
    if [ -f "$RESOLVE_MANIFEST" ]; then
        ruby "$RESOLVE_MANIFEST" "$MANIFEST_FILE" "${CUDA_PROFILE:-}" json
    else
        ruby -rjson -ryaml -e 'puts JSON.generate(YAML.load_file(ARGV.fetch(0)))' "$MANIFEST_FILE"
    fi
}

prepare_dirs() {
    run mkdir -p "$ISO_DIR" "$PACKAGE_DIR" "$IMAGE_DIR" "$CONFIG_DIR"
}

prepare_ssh_keys() {
    [ -n "$SSH_PUBLIC_KEY_FILE" ] || return 0

    if [ ! -f "$SSH_PUBLIC_KEY_FILE" ]; then
        echo "SSH public key not found: ${SSH_PUBLIC_KEY_FILE}" >&2
        exit 1
    fi

    echo "Install SSH public key: ${SSH_PUBLIC_KEY_FILE} -> ${CONFIG_DIR}/ssh_authorized_keys"
    run mkdir -p "$CONFIG_DIR"
    run cp "$SSH_PUBLIC_KEY_FILE" "${CONFIG_DIR}/ssh_authorized_keys"
}

# Local path of the base ISO declared in the manifest.
base_iso_path() {
    local local_path
    local_path=$(extract_manifest_json | ruby -rjson -e 'm=JSON.parse(STDIN.read); puts m.dig("base_iso","local_path").to_s')
    [ -n "$local_path" ] || local_path="iso/ubuntu-24.04-base.iso"

    if [ -n "$BASE_ISO_PATH" ]; then
        echo "$BASE_ISO_PATH"
    elif [[ "$local_path" = /* ]]; then
        echo "$local_path"
    elif [[ "$local_path" == iso/* && "$ISO_DIR" != "${BASE_DIR}/iso" ]]; then
        echo "${ISO_DIR}/${local_path#iso/}"
    else
        echo "${BASE_DIR}/${local_path}"
    fi
}

prepare_base_iso() {
    local manifest_json source output
    manifest_json=$(extract_manifest_json)
    source=$(ruby -rjson -e 'm=JSON.parse(STDIN.read); puts m.dig("base_iso","source").to_s' <<< "$manifest_json")

    [ -n "$source" ] || return 0
    output="$(base_iso_path)"

    case "$source" in
        http://*|https://*)
            download_file "$source" "$output" "iso"
            ;;
        *)
            echo "Copy base ISO: ${source} -> ${output}"
            run mkdir -p "$(dirname "$output")"
            run cp "$source" "$output"
            ;;
    esac
}

prepare_packages() {
    extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      Array(m["packages"]).each do |p|
        next if p["download"] == false
        next unless p["url"] && p["filename"]
        url = p["url"]
        subdir = case url
          when /cuda12|12\.8/ then "cuda12"
          when /cuda13|13\.2/ then "cuda13"
          else "base-packages"
        end
        puts [url, p["filename"], subdir].join("\t")
      end
    ' | while IFS=$'\t' read -r url filename subdir; do
        [ -n "$url" ] || continue
        [ -n "$filename" ] || continue
        download_file "$url" "${PACKAGE_DIR}/${filename}" "$subdir"
    done
}

# Target architecture of the manifest (platform.arch / install_environment.TARGET_ARCH).
manifest_target_arch() {
    extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      arch = m.dig("platform", "arch") || m.dig("install_environment", "TARGET_ARCH")
      puts arch.to_s
    '
}

# Cross-architecture builds (e.g. arm64 ISO on an amd64 host) must not resolve
# against the host apt database. Build an isolated apt root that only knows the
# target architecture and the Ubuntu ports archive, and route apt-cache /
# apt-get through it via APT_OPTS.
APT_OPTS=()
APT_CACHE_SUFFIX=""
APT_ROOT=""
setup_target_apt() {
    local target_arch host_arch apt_root mirror suite
    target_arch="$(manifest_target_arch)"
    host_arch="$(dpkg --print-architecture)"
    if [ -z "$target_arch" ] || [ "$target_arch" = "$host_arch" ]; then
        return 0
    fi

    mirror="${PORTS_MIRROR:-http://ports.ubuntu.com/ubuntu-ports}"
    suite="${UBUNTU_SUITE:-noble}"
    apt_root="${CI_WORK_DIR}/apt-${target_arch}"
    APT_ROOT="$apt_root"
    APT_CACHE_SUFFIX="-${target_arch}"
    APT_OPTS=(
        -o "Dir=${apt_root}"
        -o "Dir::State::status=${apt_root}/var/lib/dpkg/status"
        -o "APT::Architecture=${target_arch}"
        -o "APT::Architectures::=${target_arch}"
        -o "Acquire::Languages=none"
    )

    echo "Cross-arch APT: host=${host_arch} target=${target_arch} mirror=${mirror} suite=${suite}"
    # The isolated apt root only lives under CI_WORK_DIR, so it is created and
    # refreshed even in --dry-run to make the dependency closure visible.
    mkdir -p "${apt_root}/etc/apt/apt.conf.d" "${apt_root}/etc/apt/preferences.d" \
        "${apt_root}/etc/apt/sources.list.d" "${apt_root}/var/lib/apt/lists/partial" \
        "${apt_root}/var/cache/apt/archives/partial" "${apt_root}/var/lib/dpkg"
    : > "${apt_root}/var/lib/dpkg/status"
    cat > "${apt_root}/etc/apt/sources.list" <<EOF
deb [arch=${target_arch} signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] ${mirror} ${suite} main restricted universe multiverse
deb [arch=${target_arch} signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] ${mirror} ${suite}-updates main restricted universe multiverse
deb [arch=${target_arch} signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] ${mirror} ${suite}-security main restricted universe multiverse
EOF
    apt-get "${APT_OPTS[@]}" update
}

# Seed the isolated apt root with the dpkg status of the system the base ISO
# installs, so the solver only adds what the target is actually missing.
seed_target_status() {
    local iso tmp layer count
    iso="$(base_iso_path)"
    if [ ! -f "$iso" ]; then
        echo "⚠️ Base ISO not found (${iso}); resolving against an empty system."
        return 0
    fi
    if ! command -v unsquashfs >/dev/null 2>&1 || ! command -v xorriso >/dev/null 2>&1; then
        echo "⚠️ unsquashfs/xorriso missing (apt install squashfs-tools xorriso); resolving against an empty system."
        return 0
    fi
    tmp="$(mktemp -d)"
    for layer in ubuntu-server-minimal.ubuntu-server.squashfs ubuntu-server-minimal.squashfs; do
        rm -rf "${tmp:?}/root" "${tmp:?}/layer.squashfs"
        if xorriso -osirrox on -indev "$iso" -extract "/casper/${layer}" "${tmp}/layer.squashfs" >/dev/null 2>&1 &&
            unsquashfs -q -d "${tmp}/root" "${tmp}/layer.squashfs" var/lib/dpkg/status >/dev/null 2>&1 &&
            [ -s "${tmp}/root/var/lib/dpkg/status" ]; then
            cp "${tmp}/root/var/lib/dpkg/status" "${APT_ROOT}/var/lib/dpkg/status"
            count=$(grep -c '^Package:' "${APT_ROOT}/var/lib/dpkg/status")
            echo "Seeded target dpkg status from ${layer} (${count} installed packages)."
            break
        fi
    done
    chmod -R u+w "$tmp" 2>/dev/null || true
    rm -rf "$tmp"
}

# Cross-architecture builds resolve one consistent installation with apt's
# solver instead of collecting every dependency alternative: init_system.sh
# installs every offline deb in one transaction, so alternatives that conflict
# (make/make-guile, busybox/busybox-static, signed/unsigned kernel images) or
# missing dependencies of directly downloaded debs break first boot.
prepare_target_apt_solution() {
    local names="$1"
    local local_dir="${APT_ROOT}/local" inner_root="${APT_ROOT}/inner"
    local direct_list manifest_debs solution keep cache_dir deb pkg ver origin file pin_names repo_ver blocked
    local -a direct_names=()
    direct_list="$(mktemp)"; manifest_debs="$(mktemp)"; solution="$(mktemp)"; keep="$(mktemp)"
    cache_dir="${CACHE_BASE_DIR}/base-packages${APT_CACHE_SUFFIX}"

    seed_target_status

    # Directly downloaded debs become a pinned local repository; repository
    # bootstrap debs (CUDA local repo) contribute their inner index instead.
    rm -rf "$local_dir" "$inner_root"
    mkdir -p "$local_dir" "$inner_root"
    : > "${APT_ROOT}/etc/apt/sources.list.d/metal-deployer-local.list"
    : > "${APT_ROOT}/etc/apt/preferences.d/metal-deployer.pref"
    : > "${APT_ROOT}/etc/apt/preferences.d/metal-deployer-cuda-repo.pref"
    extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      Array(m["packages"]).each do |p|
        next if p["download"] == false
        next unless p["url"] && p["filename"].to_s.end_with?(".deb")
        puts p["filename"]
      end
    ' | while read -r file; do
        [ -f "${PACKAGE_DIR}/${file}" ] || continue
        echo "$file" >> "$manifest_debs"
        pkg="$(dpkg-deb -f "${PACKAGE_DIR}/${file}" Package 2>/dev/null || true)"
        [ -n "$pkg" ] || continue
        case "$pkg" in
            cuda-repo-*-local)
                # 13.0.3-580.126.20-1 bundles driver 580.126.20; toolkit-only
                # repos such as 13.4.3-1 bundle none, so nothing is blocked.
                repo_ver="$(dpkg-deb -f "${PACKAGE_DIR}/${file}" Version)"
                case "$repo_ver" in
                    *-*-*) repo_ver="${repo_ver#*-}"; repo_ver="${repo_ver%-*}" ;;
                    *) repo_ver="" ;;
                esac
                mkdir -p "${inner_root}/${pkg}"
                dpkg-deb --fsys-tarfile "${PACKAGE_DIR}/${file}" |
                    tar -x -C "${inner_root}/${pkg}" --wildcards --no-anchored 'Packages' 2>/dev/null || true
                find "${inner_root}/${pkg}" -name Packages -printf '%h\n' | while read -r dir; do
                    echo "deb [trusted=yes] file:${dir} ./" >> "${APT_ROOT}/etc/apt/sources.list.d/metal-deployer-local.list"
                    # Its bundled driver (e.g. 580.126.20) must never be chosen.
                    # apt rejects "Package: *" with a version pin, so list names.
                    blocked=$(awk -v v="$repo_ver" '/^Package:/{p=$2} /^Version:/{sub(/^[0-9]+:/, "", $2); if (v != "" && index($2, v) == 1) print p}' "${dir}/Packages" | sort -u | tr '\n' ' ')
                    if [ -n "$blocked" ]; then
                        printf 'Package: %s\nPin: version %s*\nPin-Priority: -1\n\n' "$blocked" "$repo_ver" >> "${APT_ROOT}/etc/apt/preferences.d/metal-deployer.pref"
                    fi
                    # The CUDA repo is registered only in the offline_debs stage;
                    # earlier stages (base_packages: dkms, build tools) install
                    # from the plain offline repo. Prefer Ubuntu builds for
                    # anything Ubuntu also ships, use the CUDA repo only for
                    # what it alone provides (cuda-*, libcufile, ...).
                    awk -v v="$repo_ver" '/^Package:/{p=$2} /^Version:/{ver=$2; x=ver; sub(/^[0-9]+:/, "", x); if (v == "" || index(x, v) != 1) printf "Package: %s\nPin: version %s\nPin-Priority: 100\n\n", p, ver}' \
                        "${dir}/Packages" >> "${APT_ROOT}/etc/apt/preferences.d/metal-deployer-cuda-repo.pref"
                done
                ;;
            *)
                ln -f "${PACKAGE_DIR}/${file}" "${local_dir}/" 2>/dev/null || cp "${PACKAGE_DIR}/${file}" "${local_dir}/"
                printf '%s\t%s\n' "$pkg" "$file" >> "$direct_list"
                ;;
        esac
    done
    (cd "$local_dir" && dpkg-scanpackages -m . /dev/null > Packages 2>/dev/null)
    echo "deb [trusted=yes] file:${local_dir} ./" >> "${APT_ROOT}/etc/apt/sources.list.d/metal-deployer-local.list"
    mapfile -t direct_names < <(cut -f1 "$direct_list" | sort -u)
    pin_names="${direct_names[*]}"
    printf 'Package: %s\nPin: origin ""\nPin-Priority: 1001\n' "$pin_names" >> "${APT_ROOT}/etc/apt/preferences.d/metal-deployer.pref"
    apt-get "${APT_OPTS[@]}" update >/dev/null

    local cuda_toolkit
    cuda_toolkit="$(extract_manifest_json | ruby -rjson -e 'm = JSON.parse(STDIN.read); puts m.dig("install_environment", "CUDA_TOOLKIT_PACKAGE").to_s')"

    echo "Solve offline package set: $(wc -w <<< "$names") requested + ${#direct_names[@]} direct debs${cuda_toolkit:+ + ${cuda_toolkit}}"
    # shellcheck disable=SC2086
    if ! apt-get "${APT_OPTS[@]}" -s install --no-install-recommends $names "${direct_names[@]}" $cuda_toolkit > "${APT_ROOT}/solution.log" 2>&1; then
        cat "${APT_ROOT}/solution.log" >&2
        echo "❌ The requested offline package set cannot be installed together; fix the manifest." >&2
        exit 1
    fi
    if grep -q '^Remv ' "${APT_ROOT}/solution.log"; then
        echo "⚠️ The solution removes base packages:"; grep '^Remv ' "${APT_ROOT}/solution.log"
    fi
    # Inst <pkg> [<old>] (<version> <origin> [<arch>]); origin "localhost" means
    # one of the local file repositories (direct debs or the CUDA local repo).
    sed -nE 's/^Inst ([^ ]+) (\[[^]]*\] )?\(([^ ]+) ([^ ]+) .*/\1\t\3\t\4/p' "${APT_ROOT}/solution.log" | sort -u > "$solution"
    echo "Solution: $(wc -l < "$solution") packages to install on top of the base system."

    cat "$manifest_debs" > "$keep"
    while IFS=$'\t' read -r pkg ver origin; do
        # Already downloaded from the manifest (same version may also be
        # listed under the archive origin), or served by a local repository
        # shipped in the ISO such as the CUDA local repo.
        grep -qxF "$pkg" <(printf '%s\n' "${direct_names[@]}") && continue
        [ "$origin" = "localhost" ] && continue
        file="${pkg}_${ver//:/%3a}_"
        if ls "${PACKAGE_DIR}/${file}"*.deb >/dev/null 2>&1; then
            basename -a "${PACKAGE_DIR}/${file}"*.deb >> "$keep"
            continue
        fi
        if ls "${cache_dir}/${file}"*.deb >/dev/null 2>&1; then
            echo "Cache hit APT package: ${pkg}=${ver}"
            run cp "${cache_dir}/${file}"*.deb "$PACKAGE_DIR"/
            basename -a "${cache_dir}/${file}"*.deb >> "$keep"
            continue
        fi
        echo "Download APT package: ${pkg}=${ver}"
        if [ "$DRY_RUN" = "true" ]; then
            continue
        fi
        if ! (cd "$PACKAGE_DIR" && apt-get "${APT_OPTS[@]}" -o Acquire::Retries=5 download "${pkg}=${ver}"); then
            echo "❌ Failed to download ${pkg}=${ver}."
            exit 1
        fi
        for deb in "${PACKAGE_DIR}/${file}"*.deb; do
            basename "$deb" >> "$keep"
            store_to_cache "${cache_dir}/$(basename "$deb")" "$deb"
        done
    done < "$solution"

    # init_system.sh installs every deb in packages/ in one transaction, so a
    # leftover from an earlier run (old closure version, or a manifest entry
    # whose version changed, e.g. DCGM 4.6.1 next to 4.7.1) breaks first boot.
    # Keep exactly the manifest debs plus this solution.
    for deb in "${PACKAGE_DIR}"/*.deb; do
        [ -f "$deb" ] || continue
        file="$(basename "$deb")"
        grep -qxF "$file" "$keep" && continue
        echo "Prune stale package: ${file}"
        run rm -f "$deb"
    done
    rm -f "$direct_list" "$manifest_debs" "$solution" "$keep"
}

prepare_apt_package_closure() {
    local packages
    packages=$(extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      names = []
      Array(m["packages"]).each do |p|
        if p["install_method"] == "apt" && p["packages"].is_a?(Array)
          names.concat(p["packages"])
        end
        Array(p["build_deps"]).each do |dep|
          next if ["mpi", "nccl", "cuda"].include?(dep)
          names << dep
        end
      end
      # Packages installed by name on the target (platform kernel during
      # autoinstall, signed driver on first boot) need their full dependency
      # closure offline too; the pinned debs themselves are already downloaded
      # and are skipped below by file name.
      env = m["install_environment"] || {}
      %w[KERNEL_PACKAGE NVIDIA_DRIVER_PACKAGES].each do |key|
        names.concat(env[key].to_s.split)
      end
      puts names.compact.uniq.sort.join(" ")
    ')

    [ -n "$packages" ] || return 0
    require_tool apt-cache
    require_tool apt-get
    setup_target_apt

    if [ -n "$APT_ROOT" ]; then
        prepare_target_apt_solution "$packages"
        return
    fi

    echo "Resolve APT dependency closure for offline first-boot packages..."
    # shellcheck disable=SC2086
    apt-cache "${APT_OPTS[@]}" depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances $packages \
        | awk '/^[[:alnum:]][[:alnum:].+:-]*$/ {print $1}' \
        | sort -u \
        | while read -r package; do
            [ -n "$package" ] || continue
            if ls "${PACKAGE_DIR}/${package}_"*.deb >/dev/null 2>&1; then
                echo "Skip existing APT package: ${package}"
                for deb in "${PACKAGE_DIR}/${package}_"*.deb; do
                    [ -f "$deb" ] || continue
                    cache_path="${CACHE_BASE_DIR}/base-packages${APT_CACHE_SUFFIX}/$(basename "$deb")"
                    if [ -n "$CACHE_BASE_DIR" ] && [ ! -f "$cache_path" ]; then
                        store_to_cache "$cache_path" "$deb"
                    fi
                done
                continue
            fi
            cache_hit=false
            cache_dirs=("base-packages${APT_CACHE_SUFFIX}")
            if [ -n "${CUDA_PROFILE:-}" ]; then
                cache_dirs+=("cuda${CUDA_PROFILE#cuda}")
            fi
            for cache_dir in "${cache_dirs[@]}"; do
                if ls "${CACHE_BASE_DIR}/${cache_dir}/${package}_"*.deb >/dev/null 2>&1; then
                    echo "Cache hit APT package: ${package}"
                    cp "${CACHE_BASE_DIR}/${cache_dir}/${package}_"*.deb "$PACKAGE_DIR"/
                    cache_hit=true
                    break
                fi
            done
            if [ "$cache_hit" = "true" ]; then
                continue
            fi
            echo "Download APT package: ${package}"
            if [ "$DRY_RUN" = "true" ]; then
                echo "[dry-run] apt-get download ${package}"
            else
                before_files="$(find "$PACKAGE_DIR" -maxdepth 1 -name "${package}_*.deb" -printf '%f\n' 2>/dev/null || true)"
                local success=false
                for attempt in {1..3}; do
                    if (cd "$PACKAGE_DIR" && apt-get "${APT_OPTS[@]}" -o Acquire::Retries=5 download "$package"); then
                        success=true
                        break
                    fi
                    echo "⚠️ Download ${package} failed (attempt ${attempt}/3). Retrying in 2s..."
                    sleep 2
                done
                if [ "$success" = "false" ]; then
                    echo "❌ Failed to download ${package} after 3 attempts."
                    exit 1
                fi
                find "$PACKAGE_DIR" -maxdepth 1 -name "${package}_*.deb" -print | while read -r deb; do
                    [ -n "$deb" ] || continue
                    if ! grep -qxF "$(basename "$deb")" <<< "$before_files"; then
                        store_to_cache "${CACHE_BASE_DIR}/base-packages${APT_CACHE_SUFFIX}/$(basename "$deb")" "$deb"
                    fi
                done
            fi
        done
}

prepare_docker_images() {
    [ "$DOWNLOAD_IMAGES" = "true" ] || return 0

    if ! command -v docker >/dev/null 2>&1; then
        echo "Docker not found; skip Docker image preparation." >&2
        return 0
    fi

    # Fields per image: image, filename, platform, digest, download.
    # "-" marks an unset field because tab is an IFS whitespace character and
    # consecutive tabs would otherwise collapse.
    extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      Array(m["docker_images"]).each do |img|
        next unless img["image"] && img["filename"]
        fields = [img["image"], img["filename"], img["platform"], img["digest"], img.fetch("download", true).to_s]
        puts fields.map { |v| v.nil? || v.to_s.empty? ? "-" : v.to_s }.join("\t")
      end
    ' | while IFS=$'\t' read -r image filename platform digest download; do
        local output="${PACKAGE_DIR}/${filename}"
        local pull_ref="$image"
        local platform_args=()

        if [ "$download" = "false" ]; then
            echo "Skip Docker image (download: false): ${image}"
            continue
        fi
        [ "$platform" != "-" ] && platform_args=(--platform "$platform")
        # Pin by digest but keep the human-readable tag in the saved archive so
        # load_docker_images.sh restores a usable name on the target node.
        if [ "$digest" != "-" ]; then
            pull_ref="$(image_repository "$image")@${digest}"
        fi

        if [ "$DRY_RUN" = "true" ]; then
            validate_docker_image "$pull_ref"
            echo "[dry-run] Would pull ${pull_ref} ${platform_args[*]} and save as ${image} to ${output}"
            continue
        fi

        if [ -f "$output" ] && [ "$FORCE" != "true" ]; then
            echo "Skip existing Docker image archive: ${output}"
            continue
        fi

        echo "Pull Docker image: ${pull_ref} ${platform_args[*]}"
        run docker pull "${platform_args[@]}" "$pull_ref"
        if [ "$pull_ref" != "$image" ]; then
            run docker tag "$pull_ref" "$image"
        fi
        if [ "$platform" != "-" ]; then
            local got
            # containerd image store keeps the whole index under the tag, so
            # inspect/save must be platform-scoped (Docker >= 28); older
            # classic-store Docker only has the pulled platform anyway.
            if docker image inspect --help 2>/dev/null | grep -q -- '--platform'; then
                got="$(docker image inspect --platform "$platform" -f '{{.Os}}/{{.Architecture}}' "$image")"
            else
                got="$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "$image")"
            fi
            if [ "${got}" != "${platform%%/v*}" ]; then
                echo "❌ ${image} is ${got}, expected ${platform}" >&2
                return 1
            fi
        fi
        run mkdir -p "$(dirname "$output")"
        echo "Save Docker image: ${image} -> ${output}"
        if [ "$platform" != "-" ] && docker save --help 2>/dev/null | grep -q -- '--platform'; then
            run docker save --platform "$platform" -o "$output" "$image"
        else
            run docker save -o "$output" "$image"
        fi
    done
}

# Strip the tag from an image reference (registry ports contain ":" too).
image_repository() {
    local ref="${1%@*}"
    local last="${ref##*/}"
    if [[ "$last" == *:* ]]; then
        echo "${ref%:*}"
    else
        echo "$ref"
    fi
}

main() {
    require_tool ruby
    require_tool curl

    if [ ! -f "$MANIFEST_FILE" ]; then
        echo "Manifest not found: ${MANIFEST_FILE}" >&2
        exit 1
    fi

    prepare_dirs
    prepare_ssh_keys
    prepare_base_iso
    prepare_packages
    prepare_apt_package_closure
    prepare_docker_images

    echo "Prepare finished."
    echo "Next: cd ${BASE_DIR} && sudo BUILD_WORK_DIR=${CI_WORK_DIR}/build_workspace ./build.sh"
}

main "$@"
