#!/usr/bin/env bash
# Fetch a pinned Linux kernel source tree for the live_vm walking-skeleton fixture (#26).
#
# Reproducible: the kernel ref is pinned (KDIVE_KERNEL_REF, default below) so a re-run yields
# the same tree. Idempotent: an existing checkout at the destination is left in place. The
# gated integration test (tests/integration/test_walking_skeleton.py) points KDIVE_KERNEL_SRC
# at the destination; its preflight skips with this script's name when the tree is absent.
#
# Usage: fetch-kernel-tree.sh [DEST_DIR]
#   DEST_DIR  destination for the checkout (default: ./.live-vm/linux)
# Env:
#   KDIVE_KERNEL_REPO  git URL (default: the mainline tree)
#   KDIVE_KERNEL_REF   tag/branch/sha to check out (default: v6.9)
set -euo pipefail

readonly DEFAULT_REPO="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
readonly DEFAULT_REF="v6.9"

main() {
  local dest="${1:-./.live-vm/linux}"
  local repo="${KDIVE_KERNEL_REPO:-$DEFAULT_REPO}"
  local ref="${KDIVE_KERNEL_REF:-$DEFAULT_REF}"

  if [[ -d "$dest/.git" ]]; then
    echo "kernel tree already present at $dest; leaving as-is (idempotent)" >&2
    return 0
  fi

  if ! command -v git >/dev/null 2>&1; then
    echo "error: git is required to fetch the kernel tree" >&2
    return 1
  fi

  echo "cloning $repo @ $ref into $dest (shallow)" >&2
  mkdir -p "$(dirname "$dest")"
  git clone --depth 1 --branch "$ref" "$repo" "$dest"
  echo "kernel tree ready: $dest (ref $ref)" >&2
}

main "$@"
