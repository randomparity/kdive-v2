#!/usr/bin/env bash
# Report whether this host can run the local-libvirt provider. Report-only: never
# installs, never escalates. Each runtime probe is a small function so tests can drive
# pass/fail via PATH stubs (virsh, id) and the KDIVE_KVM_NODE override. Exit 1 if any
# required check fails. Run before deploying; the service `doctor` covers post-deploy.
set -euo pipefail

readonly KVM_NODE="${KDIVE_KVM_NODE:-/dev/kvm}"
fail=0

note_fail() {
  printf "FAIL: %s\n" "$1" >&2
  printf "  fix: %s\n" "$2" >&2
  fail=1
}

_has_kvm() { [[ -r "${KVM_NODE}" && -w "${KVM_NODE}" ]]; }
_cmd() { command -v "$1" >/dev/null 2>&1; }
_in_libvirt_group() { [[ " $(id -nG 2>/dev/null) " == *" libvirt "* ]]; }
_virsh_connects() { virsh -c qemu:///system list >/dev/null 2>&1; }
_default_net_active() {
  local out
  out="$(virsh -c qemu:///system net-info default 2>/dev/null || true)"
  [[ "$out" == *"Active:"*[Yy]es* ]]
}

_has_kvm || note_fail "${KVM_NODE} not readable/writable (KVM unavailable)" \
  "enable virtualization in BIOS and load kvm modules; ensure your user can access ${KVM_NODE}"
for c in virsh qemu-system-x86_64 qemu-img; do
  _cmd "$c" || note_fail "$c not found on PATH" "install it via your distribution (see scripts/check-setup-deps.sh hints)"
done
_in_libvirt_group || note_fail "invoking user is not in the 'libvirt' group" \
  "sudo usermod -aG libvirt \"\$USER\" and re-login"
if _cmd virsh; then
  _virsh_connects || note_fail "cannot connect to qemu:///system" \
    "start the libvirt daemon: systemctl enable --now virtqemud.socket (or libvirtd)"
  _default_net_active || note_fail "libvirt 'default' network is not active" \
    "virsh -c qemu:///system net-start default && virsh -c qemu:///system net-autostart default"
fi

if ((fail)); then
  printf "\nlocal-libvirt host is NOT ready (see failures above)\n" >&2
  exit 1
fi
printf "local-libvirt host is ready\n"
