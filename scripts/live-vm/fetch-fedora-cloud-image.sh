#!/usr/bin/env bash
set -euo pipefail

RELEASE="${KDIVE_FEDORA_CLOUD_RELEASE:-43}"
ARCH="${KDIVE_FEDORA_CLOUD_ARCH:-x86_64}"
DEST="${KDIVE_FEDORA_CLOUD_IMAGE:-/var/lib/kdive/rootfs/local/fedora-cloud-${RELEASE}.qcow2}"
URL="${KDIVE_FEDORA_CLOUD_IMAGE_URL:-}"
SHA256="${KDIVE_FEDORA_CLOUD_IMAGE_SHA256:-}"

if [[ -e "${DEST}" ]]; then
  echo "fedora cloud image already present at ${DEST}; leaving as-is." >&2
  exit 0
fi

if [[ -z "${URL}" ]]; then
  echo "error: KDIVE_FEDORA_CLOUD_IMAGE_URL is required for the first fetch." >&2
  echo "       Set it to the Fedora Cloud qcow2 image URL for release ${RELEASE}/${ARCH}." >&2
  exit 1
fi

for tool in curl qemu-img; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is required to fetch ${DEST}" >&2
    exit 1
  fi
done

parent="$(realpath -m -- "$(dirname -- "${DEST}")")"
mkdir -p "${parent}"
tmp="${DEST}.part"
trap 'rm -f "${tmp}"' EXIT

curl --fail --location --output "${tmp}" "${URL}"
if [[ -n "${SHA256}" ]]; then
  actual="$(sha256sum "${tmp}" | awk '{print $1}')"
  if [[ "${actual}" != "${SHA256}" ]]; then
    echo "error: checksum mismatch for ${URL}" >&2
    echo "       expected ${SHA256}" >&2
    echo "       actual   ${actual}" >&2
    exit 1
  fi
fi

qemu-img info --output=json "${tmp}" >/dev/null
chmod 0644 "${tmp}"
mv "${tmp}" "${DEST}"
echo "fedora cloud image ready at ${DEST}" >&2
