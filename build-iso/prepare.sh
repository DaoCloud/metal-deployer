#!/bin/bash
set -euo pipefail

BASE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CI_WORK_DIR="${CI_WORK_DIR:-${BASE_DIR}/.ci-work}"
MANIFEST_FILE="${MANIFEST_FILE:-${BASE_DIR}/manifest.yaml}"
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
  --manifest PATH          Manifest path. Default: build-iso/manifest.yaml
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

prepare_base_iso() {
    local manifest_json source local_path output
    manifest_json=$(extract_manifest_json)
    source=$(ruby -rjson -e 'm=JSON.parse(STDIN.read); puts m.dig("base_iso","source").to_s' <<< "$manifest_json")
    local_path=$(ruby -rjson -e 'm=JSON.parse(STDIN.read); puts m.dig("base_iso","local_path").to_s' <<< "$manifest_json")

    [ -n "$source" ] || return 0
    [ -n "$local_path" ] || local_path="iso/ubuntu-24.04-base.iso"

    if [ -n "$BASE_ISO_PATH" ]; then
        output="$BASE_ISO_PATH"
    elif [[ "$local_path" = /* ]]; then
        output="$local_path"
    elif [[ "$local_path" == iso/* && "$ISO_DIR" != "${BASE_DIR}/iso" ]]; then
        output="${ISO_DIR}/${local_path#iso/}"
    else
        output="${BASE_DIR}/${local_path}"
    fi

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
      puts names.compact.uniq.sort.join(" ")
    ')

    [ -n "$packages" ] || return 0
    require_tool apt-cache
    require_tool apt-get

    echo "Resolve APT dependency closure for offline first-boot packages..."
    # shellcheck disable=SC2086
    apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
        --no-breaks --no-replaces --no-enhances $packages \
        | awk '/^[[:alnum:]][[:alnum:].+:-]*$/ {print $1}' \
        | sort -u \
        | while read -r package; do
            [ -n "$package" ] || continue
            if ls "${PACKAGE_DIR}/${package}_"*.deb >/dev/null 2>&1; then
                echo "Skip existing APT package: ${package}"
                for deb in "${PACKAGE_DIR}/${package}_"*.deb; do
                    [ -f "$deb" ] || continue
                    cache_path="${CACHE_BASE_DIR}/base-packages/$(basename "$deb")"
                    if [ -n "$CACHE_BASE_DIR" ] && [ ! -f "$cache_path" ]; then
                        store_to_cache "$cache_path" "$deb"
                    fi
                done
                continue
            fi
            cache_hit=false
            cache_dirs=(base-packages)
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
                    if (cd "$PACKAGE_DIR" && apt-get -o Acquire::Retries=5 download "$package"); then
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
                        store_to_cache "${CACHE_BASE_DIR}/base-packages/$(basename "$deb")" "$deb"
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

    extract_manifest_json | ruby -rjson -e '
      m = JSON.parse(STDIN.read)
      Array(m["docker_images"]).each do |img|
        next unless img["image"] && img["filename"]
        puts [img["image"], img["filename"]].join("\t")
      end
    ' | while IFS=$'\t' read -r image filename; do
        local output="${PACKAGE_DIR}/${filename}"
        if [ "$DRY_RUN" = "true" ]; then
            validate_docker_image "$image"
            echo "[dry-run] Would save Docker image to ${output}"
            continue
        fi

        if [ -f "$output" ] && [ "$FORCE" != "true" ]; then
            echo "Skip existing Docker image archive: ${output}"
            continue
        fi

        echo "Pull Docker image: ${image}"
        run docker pull "$image"
        run mkdir -p "$(dirname "$output")"
        echo "Save Docker image: ${image} -> ${output}"
        run docker save -o "$output" "$image"
    done
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
