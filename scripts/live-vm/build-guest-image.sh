#!/usr/bin/env bash
# Build a bootable kdive-ready Fedora rootfs qcow2 — fully unprivileged (ADR-0052, #124).
#
# Two unprivileged libguestfs stages:
#   1. virt-builder customizes a Fedora scratch image (sshd, authorized key, a kdive-ready
#      serial unit that echoes the readiness marker to /dev/ttyS0).
#   2. virt-tar-out + virt-make-fs --type=ext4 repack the root tree into a no-partition-table
#      whole-disk ext4 qcow2 — the only layout the direct-kernel boot provider mounts
#      (root=/dev/vda, no initramfs, ADR-0030). /etc/fstab is then normalized to a lone "/"
#      entry and /etc/crypttab removed, because the scratch image's GPT-layout mount entries
#      would stall local-fs.target and the kdive-ready marker would never fire.
#
# Guest-internal SELinux is disabled (guest /etc/selinux/config) so the host-written
# authorized_keys is read without a relabel and the first boot does not relabel+reboot (which
# would risk a false boot timeout). This is the guest's internal SELinux only; it is independent
# of the host-side virt_image_t/0644 labeling of the image file, which still applies (see
# docs/runbooks/live-stack.md §3).
#
# Idempotent (presence-only): an existing file at the destination is left in place and the
# build is skipped — the destination is NOT validated and build inputs are NOT consulted, so a
# changed input (KDIVE_ROOTFS_DEBUG/_VMLINUX/_SSH_USER/_SIZE, or a rotated managed key) or a
# truncated image from an interrupted run is recovered by deleting the destination and re-running.
#
# No host-side sudo/pkexec. The output directory is pre-prepared by an OS admin for the default
# root-owned path (docs/runbooks/live-stack.md §3); the per-build write and final chmod 0644 are
# unprivileged. The chmod 0644 lets the separate qemu user read the image under qemu:///system.
set -euo pipefail

ROOTFS_PATH="${KDIVE_ROOTFS:-/var/lib/kdive/rootfs/local/fedora-kdive-ready-43.qcow2}"
RELEASEVER="${KDIVE_ROOTFS_RELEASEVER:-43}"
# The Stage-1 `virt-builder --size` must be >= the template's virtual size, or Stage 1 fails
# with "images cannot be shrunk". 6G is the Fedora-43 floor; raise KDIVE_ROOTFS_SIZE for a
# larger template or when staging a vmlinux.
IMAGE_SIZE="${KDIVE_ROOTFS_SIZE:-6G}"
SSH_USER="${KDIVE_ROOTFS_SSH_USER:-root}"
# Debug-ready additions: install drgn (+ kexec-tools/makedumpfile) and, optionally, stage a
# paired vmlinux into the guest debug path so live drgn introspection is turnkey. Staging a
# vmlinux implies the debug tooling.
DEBUG_READY="${KDIVE_ROOTFS_DEBUG:-0}"
VMLINUX_PATH="${KDIVE_ROOTFS_VMLINUX:-}"
KERNEL_RELEASE="${KDIVE_ROOTFS_KERNEL_RELEASE:-}"
MARKER="kdive-ready"

# The first positional argument overrides KDIVE_ROOTFS.
if [[ $# -ge 1 ]]; then
  ROOTFS_PATH="$1"
fi

# ORDERING INVARIANT: nothing above the idempotency guard may run an EXTERNAL command. Only
# parameter expansion and bash builtins ([[ ]], echo, exit) are allowed, so a second invocation
# on an existing image is a no-op even with an empty PATH (the fixtures-test idempotency contract).
# SCRIPT_DIR/REPO_ROOT are therefore computed lower down (they shell out to `dirname`), and the
# Stage-0 preflight (realpath/mkdir) sits after the guard too.

# Idempotency guard (presence-only): it does not validate the file or consult build inputs. The
# `-f` test follows symlinks, so a symlink pointing at a regular file short-circuits here before
# the Stage-0 symlink refusal below — safe, because this branch performs no write.
if [[ -f "${ROOTFS_PATH}" ]]; then
  echo "rootfs image already present at ${ROOTFS_PATH}; leaving as-is (idempotent)." >&2
  echo "       No rebuild performed and build inputs were not consulted; delete the file to" >&2
  echo "       force a rebuild (e.g. after changing KDIVE_ROOTFS_DEBUG/_VMLINUX/_SSH_USER/_SIZE" >&2
  echo "       or rotating the managed SSH key)." >&2
  exit 0
fi

# Validate the guest username before it reaches a `virt-builder --run-command` guest shell or the
# colon-delimited `--ssh-inject "user:file:key"` selector. Restricting to the useradd NAME_REGEX
# envelope (lowercase-start, max 32) means the value cannot carry shell metacharacters or a stray
# ':' that would misparse the ssh-inject format.
if [[ ! "${SSH_USER}" =~ ^[a-z_][a-z0-9_-]*$ || ${#SSH_USER} -gt 32 ]]; then
  echo "error: KDIVE_ROOTFS_SSH_USER='${SSH_USER}' is not a valid username." >&2
  echo "       Allowed: ^[a-z_][a-z0-9_-]*\$, at most 32 characters." >&2
  exit 1
fi

# Stage-0 output-dir preflight (before any libguestfs tool, so a missing/unwritable output dir
# fails in seconds rather than after the minutes-long Stage 1). Refuse a pre-existing symlink at
# the output path so later writes cannot be redirected, then canonicalize the parent while keeping
# the final component literal. The path is operator-configurable, so it is not pinned under a fixed
# base.
if [[ -L "${ROOTFS_PATH}" ]]; then
  echo "error: KDIVE_ROOTFS='${ROOTFS_PATH}' is a symlink; refusing to write through it." >&2
  exit 1
fi
rootfs_parent="$(realpath -m -- "$(dirname -- "${ROOTFS_PATH}")")"
ROOTFS_PATH="${rootfs_parent}/$(basename -- "${ROOTFS_PATH}")"
if [[ ! -d "${rootfs_parent}" ]]; then
  if ! mkdir -p "${rootfs_parent}" 2>/dev/null; then
    echo "error: output directory '${rootfs_parent}' does not exist and could not be created." >&2
    echo "       Pre-prepare it (an OS admin step for the default root-owned path; see" >&2
    echo "       docs/runbooks/live-stack.md §3) or set KDIVE_ROOTFS to a writable location." >&2
    exit 1
  fi
fi
if [[ ! -w "${rootfs_parent}" ]]; then
  echo "error: output directory '${rootfs_parent}' is not writable by the current user." >&2
  echo "       Pre-prepare it (see docs/runbooks/live-stack.md §3) or set KDIVE_ROOTFS to a" >&2
  echo "       writable location." >&2
  exit 1
fi

# Validate the optional vmlinux staging inputs before any libguestfs tool is required, so the
# failure is deterministic in environments without virt-builder.
if [[ -n "${VMLINUX_PATH}" ]]; then
  DEBUG_READY=1 # staging a vmlinux is useless without drgn target-side
  if [[ -z "${KERNEL_RELEASE}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX is set but KDIVE_ROOTFS_KERNEL_RELEASE is empty." >&2
    echo "       Set KDIVE_ROOTFS_KERNEL_RELEASE to the booted kernel's \`make kernelrelease\`" >&2
    echo "       (== guest \`uname -r\`); the vmlinux is staged at" >&2
    echo "       /usr/lib/debug/lib/modules/<release>/vmlinux and a mismatch is never read." >&2
    exit 1
  fi
  # The release is interpolated into a guest path in --mkdir/--upload; restrict it to the
  # kernelrelease character envelope so it cannot carry shell or path metacharacters, and reject
  # '.'/'..' which would relocate the staged vmlinux out of the per-release dir.
  if [[ ! "${KERNEL_RELEASE}" =~ ^[a-zA-Z0-9._+-]+$ || "${KERNEL_RELEASE}" == "." || "${KERNEL_RELEASE}" == ".." ]]; then
    echo "error: KDIVE_ROOTFS_KERNEL_RELEASE='${KERNEL_RELEASE}' is not a valid release." >&2
    echo "       Allowed: ^[a-zA-Z0-9._+-]+\$ (e.g. 7.0.0-kdive), excluding '.' and '..'." >&2
    exit 1
  fi
  if [[ -L "${VMLINUX_PATH}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX='${VMLINUX_PATH}' is a symlink; refusing to stage it." >&2
    exit 1
  fi
  if [[ ! -f "${VMLINUX_PATH}" ]]; then
    echo "error: KDIVE_ROOTFS_VMLINUX='${VMLINUX_PATH}' is not a regular file." >&2
    exit 1
  fi
  VMLINUX_PATH="$(realpath -m -- "${VMLINUX_PATH}")"
  # The vmlinux competes with the Fedora base for the Stage-2 ext4; the 6G default does not fit a
  # debug (KASAN+DWARF) vmlinux. Warn with a concrete recommendation when staging is requested at
  # the default size, rather than letting virt-make-fs fail "too small".
  if [[ -z "${KDIVE_ROOTFS_SIZE:-}" ]]; then
    vmlinux_mib=$((($(stat -c %s -- "${VMLINUX_PATH}") + 1048575) / 1048576))
    recommended_gib=$((6 + (vmlinux_mib * 12 / 10 + 1023) / 1024))
    echo "warning: staging a ${vmlinux_mib} MiB vmlinux at the default KDIVE_ROOTFS_SIZE=6G;" >&2
    echo "         this is likely too small. Set KDIVE_ROOTFS_SIZE to at least ${recommended_gib}G" >&2
    echo "         (6G base + vmlinux + ext4 overhead) to avoid a virt-make-fs 'too small' error." >&2
  fi
fi

resolve_authorized_key() {
  if [[ -n "${KDIVE_ROOTFS_AUTHORIZED_KEY:-}" ]]; then
    printf '%s\n' "${KDIVE_ROOTFS_AUTHORIZED_KEY}"
    return
  fi
  # Single source of truth for the managed key path + generation (ADR-0052). The helper ensures
  # the keypair and prints the .pub path on stdout; it names KDIVE_ROOTFS_AUTHORIZED_KEY on stderr
  # if generation fails. `|| true` is load-bearing under `set -e`: a non-zero exit in the command
  # substitution would otherwise abort before the explanatory guard below runs.
  PYTHONPATH="${REPO_ROOT}/src" python3 -m kdive.prereqs.managed_ssh_key --ensure-public-key ||
    true
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: required command '$1' not found on PATH (install libguestfs-tools)" >&2
    exit 1
  }
}

# Repo root (scripts/live-vm/ -> repo root), so the managed-key helper is importable regardless of
# the caller's cwd. Computed here (not at the top) because it shells out to `dirname` — see the
# ordering invariant above the idempotency guard. The helper is the single source of truth for the
# managed key path + generation (ADR-0052), shared with the future connect-time `ssh -i` identity.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

authorized_key="$(resolve_authorized_key)"
if [[ -z "${authorized_key}" || ! -f "${authorized_key}" ]]; then
  echo "error: could not resolve an SSH public key to install." >&2
  echo "       Set KDIVE_ROOTFS_AUTHORIZED_KEY to a .pub file, or ensure 'ssh-keygen' and" >&2
  echo "       'python3' are available so kdive can generate its managed key." >&2
  exit 1
fi

require virt-builder
require virt-tar-out
require virt-make-fs
require guestfish
require qemu-img

if ! virt-builder --list 2>/dev/null | grep -qE "^fedora-${RELEASEVER}[[:space:]]"; then
  echo "error: template 'fedora-${RELEASEVER}' is not in the libguestfs index." >&2
  echo "       Run 'virt-builder --list' to see available releases and set" >&2
  echo "       KDIVE_ROOTFS_RELEASEVER to one of them. (First use fetches the template over the" >&2
  echo "       network; ensure reachability or a pre-seeded virt-builder cache.)" >&2
  exit 1
fi

# mktemp creates each file 0600 regardless of umask, and the `>`-redirect writes below preserve
# that mode. scratch and rootfs_tar are written by external tools (virt-builder --output /
# virt-tar-out) that may unlink+recreate at the default umask; they are chmod'd 0600 right after
# each tool write so their mode is deterministic regardless of tool behavior.
unit_file="$(mktemp)"
fstab_file="$(mktemp)"
selinux_file="$(mktemp)"
scratch="$(mktemp --suffix=.qcow2)"
rootfs_tar="$(mktemp --suffix=.tar)"
cleanup() { rm -f "${unit_file}" "${fstab_file}" "${selinux_file}" "${scratch}" "${rootfs_tar}"; }
trap cleanup EXIT

cat >"${unit_file}" <<EOF
[Unit]
Description=Signal kdive serial readiness
After=dev-ttyS0.device
Wants=dev-ttyS0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo ${MARKER} > /dev/ttyS0'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

printf '/dev/vda / ext4 defaults 0 1\n' >"${fstab_file}"
printf 'SELINUX=disabled\nSELINUXTYPE=targeted\n' >"${selinux_file}"

# virt-builder runs --run-command and --ssh-inject in command-line order, and --ssh-inject
# requires the user to already exist. The useradd --run-command is therefore placed before
# --ssh-inject so a non-root SSH_USER exists when the key is injected.
builder_args=(
  "fedora-${RELEASEVER}"
  --format qcow2 --size "${IMAGE_SIZE}" --output "${scratch}"
  --install openssh-server
  --run-command 'systemctl enable sshd.service'
)
if [[ "${SSH_USER}" != "root" ]]; then
  builder_args+=(--run-command "useradd --create-home --shell /bin/bash ${SSH_USER}")
fi
builder_args+=(
  --ssh-inject "${SSH_USER}:file:${authorized_key}"
  --upload "${unit_file}:/etc/systemd/system/${MARKER}.service"
  --run-command "systemctl enable ${MARKER}.service"
)
if [[ "${DEBUG_READY}" == "1" ]]; then
  builder_args+=(--install "drgn,kexec-tools,makedumpfile")
fi
if [[ -n "${VMLINUX_PATH}" ]]; then
  guest_debug_dir="/usr/lib/debug/lib/modules/${KERNEL_RELEASE}"
  builder_args+=(
    --mkdir "${guest_debug_dir}"
    --upload "${VMLINUX_PATH}:${guest_debug_dir}/vmlinux"
  )
fi

echo "Stage 1: customizing fedora-${RELEASEVER} scratch image ..." >&2
virt-builder "${builder_args[@]}"
chmod 0600 "${scratch}"

echo "Stage 2: repacking to whole-disk ext4 ${ROOTFS_PATH} ..." >&2
virt-tar-out -a "${scratch}" / "${rootfs_tar}"
chmod 0600 "${rootfs_tar}"
virt-make-fs --type=ext4 --format=qcow2 --size="${IMAGE_SIZE}" "${rootfs_tar}" "${ROOTFS_PATH}"

# Normalize the inherited mount config and disable guest-internal SELinux. The GFEOF delimiter is
# intentionally UNQUOTED: ${fstab_file}/${selinux_file} are host-side temp paths that must expand
# so guestfish receives real filenames. The guest-side paths are fixed literals.
guestfish --rw -a "${ROOTFS_PATH}" -i <<GFEOF
upload ${fstab_file} /etc/fstab
upload ${selinux_file} /etc/selinux/config
rm-f /etc/crypttab
GFEOF

# The caller owns the file it just wrote; chmod is unprivileged. 0644 lets the separate qemu user
# read the image under qemu:///system. Re-assert the target is a regular file (not a symlink
# swapped in during the build) before chmod redirects onto it.
if [[ ! -f "${ROOTFS_PATH}" || -L "${ROOTFS_PATH}" ]]; then
  echo "error: ${ROOTFS_PATH} is not a regular file after build; refusing to chmod." >&2
  exit 1
fi
chmod 0644 "${ROOTFS_PATH}"

echo "Done: ${ROOTFS_PATH}" >&2
qemu-img info "${ROOTFS_PATH}" >&2 || true
# Print the content hash on stdout so a future publish/upload flow can content-address this
# artifact (spec §7).
sha256sum -- "${ROOTFS_PATH}" || true
