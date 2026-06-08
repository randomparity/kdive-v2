#!/usr/bin/env bash
set -euo pipefail

ROOTFS_PATH="${KDIVE_BUSYBOX_ROOTFS:-/var/lib/kdive/rootfs/local/busybox-bare.qcow2}"
IMAGE_SIZE="${KDIVE_BUSYBOX_ROOTFS_SIZE:-256M}"

if [[ -e "${ROOTFS_PATH}" ]]; then
  echo "busybox rootfs image already present at ${ROOTFS_PATH}; leaving as-is." >&2
  exit 0
fi

for tool in busybox virt-make-fs; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is required to build ${ROOTFS_PATH}" >&2
    exit 1
  fi
done

rootfs_parent="$(realpath -m -- "$(dirname -- "${ROOTFS_PATH}")")"
mkdir -p "${rootfs_parent}"
if [[ ! -w "${rootfs_parent}" ]]; then
  echo "error: output directory '${rootfs_parent}' is not writable by the current user." >&2
  exit 1
fi

scratch="$(mktemp -d)"
cleanup() {
  find "${scratch}" -depth -mindepth 1 -delete
  rmdir "${scratch}"
}
trap cleanup EXIT

mkdir -p "${scratch}"/{bin,dev,etc,proc,sys}
busybox --install -s "${scratch}/bin"
cat >"${scratch}/etc/inittab" <<'EOF'
::sysinit:/bin/mount -t proc proc /proc
::sysinit:/bin/mount -t sysfs sysfs /sys
::respawn:/bin/sh
EOF

virt-make-fs --type=ext4 --format=qcow2 --size="${IMAGE_SIZE}" "${scratch}" "${ROOTFS_PATH}"
chmod 0644 "${ROOTFS_PATH}"
echo "busybox rootfs image ready at ${ROOTFS_PATH}" >&2
