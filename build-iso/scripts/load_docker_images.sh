#!/bin/bash
set -euo pipefail

IMAGE_DIR="${IMAGE_DIR:-/opt/resource/packages/images}"

[ -d "$IMAGE_DIR" ] || exit 0

systemctl is-active --quiet docker || systemctl start docker

shopt -s nullglob
images=("$IMAGE_DIR"/*.tar "$IMAGE_DIR"/*.tar.gz "$IMAGE_DIR"/*.tgz)

if [ "${#images[@]}" -eq 0 ]; then
    echo "No docker image archives found in ${IMAGE_DIR}."
    exit 0
fi

for image in "${images[@]}"; do
    echo "Loading docker image archive: ${image}"
    docker load -i "$image"
done

docker images
