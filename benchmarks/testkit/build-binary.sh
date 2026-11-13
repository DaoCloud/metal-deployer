#!/bin/bash
# Build single-file testkit binaries (PyInstaller) for linux/amd64 and linux/arm64.
# The control machine then needs neither Python nor PyYAML.
#
# Builds run with docker buildx in a Debian bullseye image (glibc 2.31), so the binaries also run
# on older control machines; buildx emulates arm64 on an x86_64 host.
#
# Usage: benchmarks/testkit/build-binary.sh [amd64] [arm64]     (default: both)
# Env:   PYTHON_IMAGE  (default python:3.11-slim-bullseye; e.g. docker.m.daocloud.io/library/python:3.11-slim-bullseye)
#        PIP_INDEX_URL passed to pip in the build
#        BUILDER       buildx builder (default: the current one)
#        OUT           output directory (default benchmarks/testkit/dist)
# Output: $OUT/testkit-<version>-linux-<arch>, $OUT/SHA256SUMS
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=${OUT:-$HERE/dist}
IMAGE=${PYTHON_IMAGE:-python:3.11-slim-bullseye}
ARCHS=("$@")
[ ${#ARCHS[@]} -eq 0 ] && ARCHS=(amd64 arm64)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/testkit/__init__.py")
mkdir -p "$OUT"

DOCKERFILE=$(cat <<'EOF'
ARG IMAGE=python:3.11-slim-bullseye
FROM ${IMAGE} AS build
ARG NAME
ARG PIP_INDEX_URL=""
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils >/dev/null \
    && rm -rf /var/lib/apt/lists/*
RUN if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi; \
    pip install -q --root-user-action=ignore 'pyinstaller>=6,<7' 'PyYAML>=5.1'
COPY testkit /build/testkit
WORKDIR /build
RUN pyinstaller --log-level WARN --onefile --name "$NAME" --paths . \
        --add-data testkit/templates:testkit/templates \
        --add-data testkit/node_scripts:testkit/node_scripts \
        testkit/__main__.py \
    && ./dist/"$NAME" --version
FROM scratch
ARG NAME
COPY --from=build /build/dist/ /
EOF
)

for arch in "${ARCHS[@]}"; do
    name=testkit-$VERSION-linux-$arch
    echo "[build] $name"
    docker buildx build ${BUILDER:+--builder "$BUILDER"} --platform "linux/$arch" \
        --build-arg IMAGE="$IMAGE" --build-arg NAME="$name" --build-arg PIP_INDEX_URL="${PIP_INDEX_URL:-}" \
        --output "type=local,dest=$OUT" -f - "$HERE" <<<"$DOCKERFILE"
    chmod 755 "$OUT/$name"
done
(cd "$OUT" && sha256sum testkit-"$VERSION"-linux-* > SHA256SUMS && cat SHA256SUMS)
