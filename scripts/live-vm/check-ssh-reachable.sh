#!/usr/bin/env bash
# Verify the live_vm kdump guest is reachable over SSH for the M1 live introspection test (#71).
#
# The M1 live introspection criterion (introspect.run over drgn-over-SSH, ADR-0039) needs a
# guest reachable over SSH, not just a built image. This script probes the target and, on
# success, prints the export line for KDIVE_LIVE_SSH_TARGET that the gated test's preflight
# (tests/integration/test_walking_skeleton.py::_live_vm_preflight(require_ssh=True)) requires.
# It opens no transport beyond a single non-interactive SSH probe; it never logs credentials.
#
# Usage: check-ssh-reachable.sh HOST [USER]
#   HOST  the guest hostname or IP (required)
#   USER  the SSH user (default: root)
# Env:
#   KDIVE_LIVE_SSH_PORT  SSH port (default: 22)
set -euo pipefail

readonly DEFAULT_USER="root"
readonly DEFAULT_PORT="22"

usage() {
  echo "usage: check-ssh-reachable.sh HOST [USER]" >&2
}

main() {
  if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    return 1
  fi

  local host="$1"
  local user="${2:-$DEFAULT_USER}"
  local port="${KDIVE_LIVE_SSH_PORT:-$DEFAULT_PORT}"

  if ! command -v ssh >/dev/null 2>&1; then
    echo "error: ssh is required to probe the guest" >&2
    return 1
  fi

  echo "probing ${user}@${host}:${port} over ssh" >&2
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    -p "$port" "${user}@${host}" true; then
    echo "error: ${user}@${host}:${port} is not reachable over ssh" >&2
    return 1
  fi

  echo "guest reachable; export the target for the gated test:" >&2
  echo "export KDIVE_LIVE_SSH_TARGET=ssh://${user}@${host}:${port}"
}

main "$@"
