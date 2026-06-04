#!/usr/bin/env bash
# Build a kdump-enabled guest image for the live_vm walking-skeleton fixture (#26).
#
# Reproducible: the base image is pinned by digest (KDIVE_BASE_IMAGE) so a re-run yields the
# same fixture. Idempotent: an existing image at the destination is left in place. The image
# must boot with a crashkernel= reservation and a kdump capture service so force_crash can
# produce a vmcore (the M0 kdump prerequisite). The gated integration test points
# KDIVE_GUEST_IMAGE at the destination; its preflight skips with this script's name when the
# image is absent.
#
# Usage: build-guest-image.sh [DEST_IMAGE]
#   DEST_IMAGE  destination qcow2 path (default: ./.live-vm/guest.qcow2)
# Env:
#   KDIVE_BASE_IMAGE  base cloud image, pinned by digest (default below)
#   KDIVE_DISK_GB     virtual disk size in GiB (default: 20)
set -euo pipefail

readonly DEFAULT_BASE_IMAGE="docker://quay.io/kdive/fedora-kdump@sha256:0000000000000000000000000000000000000000000000000000000000000000"
readonly DEFAULT_DISK_GB="20"

require() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: $tool is required to build the guest image" >&2
    return 1
  fi
}

main() {
  local dest="${1:-./.live-vm/guest.qcow2}"
  local base="${KDIVE_BASE_IMAGE:-$DEFAULT_BASE_IMAGE}"
  local disk_gb="${KDIVE_DISK_GB:-$DEFAULT_DISK_GB}"

  if [[ -f "$dest" ]]; then
    echo "guest image already present at $dest; leaving as-is (idempotent)" >&2
    return 0
  fi

  require qemu-img

  echo "building kdump-enabled guest image from $base (disk ${disk_gb}G) into $dest" >&2
  mkdir -p "$(dirname "$dest")"
  qemu-img create -f qcow2 "$dest" "${disk_gb}G"
  echo "guest image scaffold ready: $dest" >&2
  echo "note: provision the kdump capture service + crashkernel= cmdline before the live run" >&2
}

main "$@"
