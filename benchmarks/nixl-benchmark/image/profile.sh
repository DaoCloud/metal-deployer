#!/usr/bin/env bash
# Resolve a build profile of the nixl-benchmark image.
#
#   profile.sh list                  profile names
#   profile.sh <profile> args        KEY=VALUE per Dockerfile ARG (for --build-arg / build-push-action)
#   profile.sh <profile> get <KEY>   one value, e.g. PLATFORM
#
# <profile> is a name under profiles/ (cuda13-gb300) or a path to an .env file. Values come from
# the profile; an environment variable of the same name wins. ARGs set in neither keep the
# Dockerfile default. NIXL_PROFILE is always passed so the image records its profile.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

profile_file() {
    if [ -f "$1" ]; then
        echo "$1"
    elif [ -f "${DIR}/profiles/$1.env" ]; then
        echo "${DIR}/profiles/$1.env"
    else
        echo "profile not found: $1 (available: $(list | tr '\n' ' '))" >&2
        exit 1
    fi
}

list() {
    for f in "${DIR}"/profiles/*.env; do
        basename "${f}" .env
    done
}

# Value of KEY: environment first, then the profile file.
value() {
    local key="$1" file="$2"
    if [ -n "${!key:-}" ]; then
        printf '%s\n' "${!key}"
        return
    fi
    sed -n "s/^${key}=//p" "${file}" | tail -n 1
}

[ $# -ge 1 ] || usage
if [ "$1" = list ]; then
    list
    exit 0
fi
[ $# -ge 2 ] || usage
file="$(profile_file "$1")"
name="$(basename "${file}" .env)"

case "$2" in
    args)
        for key in $(sed -n 's/^ARG \([A-Z0-9_]*\).*/\1/p' "${DIR}/Dockerfile" | sort -u); do
            [ "${key}" = NIXL_PROFILE ] && continue
            v="$(value "${key}" "${file}")"
            [ -n "${v}" ] && printf '%s=%s\n' "${key}" "${v}"
        done
        printf 'NIXL_PROFILE=%s\n' "${name}"
        ;;
    get)
        [ $# -ge 3 ] || usage
        value "$3" "${file}"
        ;;
    *)
        usage
        ;;
esac
