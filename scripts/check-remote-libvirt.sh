#!/usr/bin/env bash
# Report whether the remote-libvirt provider can reach a target host. Report-only:
# never installs, never escalates, opens no transport beyond a single ssh probe and a
# read-only `virsh list`. Pre-deploy: no System exists yet, so it checks only that the
# guest-helper FILES are staged on this host for injection — in-guest verification is
# the service `doctor`'s job, not this script's.
# Usage: check-remote-libvirt.sh HOST [USER] [URI]
# Env: KDIVE_REMOTE_SSH_PORT (default 22), KDIVE_REMOTE_PKI_DIR, KDIVE_GUEST_HELPERS_DIR
set -euo pipefail

readonly DEFAULT_USER="root"
readonly PORT="${KDIVE_REMOTE_SSH_PORT:-22}"
readonly PKI_DIR="${KDIVE_REMOTE_PKI_DIR:-/etc/pki/libvirt}"
readonly HELPERS_DIR="${KDIVE_GUEST_HELPERS_DIR:-deploy/remote-libvirt-guest-helpers}"

usage() {
  echo "usage: check-remote-libvirt.sh HOST [USER] [URI]" >&2
}

fail=0
note_fail() {
  printf "FAIL: %s\n" "$1" >&2
  printf "  fix: %s\n" "$2" >&2
  fail=1
}

main() {
  if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    return 1
  fi
  local host="$1" user="${2:-$DEFAULT_USER}"
  local uri="${3:-qemu+tls://${host}/system}"

  command -v ssh >/dev/null 2>&1 || note_fail "ssh not found" "install your distro's openssh client"
  command -v virsh >/dev/null 2>&1 || note_fail "virsh not found" "install libvirt-client (see check-setup-deps.sh)"

  if command -v ssh >/dev/null 2>&1; then
    ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
      -p "${PORT}" "${user}@${host}" true 2>/dev/null ||
      note_fail "ssh to ${user}@${host}:${PORT} failed" "ensure the host is up and your key is authorized"
  fi

  if ! { [[ -d "${PKI_DIR}" ]] && compgen -G "${PKI_DIR}/*.pem" >/dev/null 2>&1; }; then
    note_fail "no TLS PKI material in ${PKI_DIR}" "provision client cert/key — see the remote-libvirt provider guide"
  fi

  if command -v virsh >/dev/null 2>&1; then
    virsh -c "${uri}" list >/dev/null 2>&1 ||
      note_fail "cannot connect to ${uri}" "verify virtproxyd/TLS on the host and the URI"
  fi

  compgen -G "${HELPERS_DIR}/kdive-*" >/dev/null 2>&1 ||
    note_fail "guest-helper files not staged in ${HELPERS_DIR}" "ship deploy/remote-libvirt-guest-helpers/ to this host"

  if ((fail)); then
    printf "\nremote-libvirt target is NOT ready (see failures above)\n" >&2
    return 1
  fi
  printf "remote-libvirt target is ready\n"
}

main "$@"
