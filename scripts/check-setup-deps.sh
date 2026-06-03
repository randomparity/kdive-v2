#!/usr/bin/env bash
# Report host packages KDIVE needs, grouped by tier, with a single distro-specific
# install hint per tier. Reports only — never installs and never escalates. Set
# KDIVE_OS_RELEASE to point at an alternate os-release file (used by the tests).
set -euo pipefail

readonly OS_RELEASE_FILE="${KDIVE_OS_RELEASE:-/etc/os-release}"

# Per-tier accumulators: *_commands feed the human-readable summary line,
# *_packages feed the distro install hint. manual_hints holds install commands
# for tooling that distros do not package (uv, prek, just).
# The *_packages arrays are written and read only through namerefs
# (note_package / report_tier), which shellcheck cannot follow — hence the
# SC2034 "unused" suppressions on those declarations.
required_commands=()
# shellcheck disable=SC2034
required_packages=()
recommended_commands=()
# shellcheck disable=SC2034
recommended_packages=()
future_commands=()
# shellcheck disable=SC2034
future_packages=()
manual_hints=()

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

load_distro_id() {
  local id="" id_like=""
  if [[ -r "${OS_RELEASE_FILE}" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "${OS_RELEASE_FILE}"
    id="${ID:-}"
    id_like="${ID_LIKE:-}"
  fi
  case " ${id} ${id_like} " in
  *" fedora "* | *" rhel "* | *" centos "*) printf "fedora" ;;
  *" debian "* | *" ubuntu "*) printf "debian" ;;
  *" arch "*) printf "arch" ;;
  *" opensuse "* | *" suse "*) printf "opensuse" ;;
  *) printf "unknown" ;;
  esac
}

# Map a logical dependency name to its package name on the detected distro.
# Names that are identical everywhere fall through to the default branch.
package_for() {
  local name="$1" distro="$2"
  case "${name}:${distro}" in
  pkg-config:fedora) printf "pkgconf-pkg-config" ;;
  pkg-config:arch) printf "pkgconf" ;;
  libvirt-headers:fedora | libvirt-headers:opensuse) printf "libvirt-devel" ;;
  libvirt-headers:arch) printf "libvirt" ;;
  libvirt-headers:*) printf "libvirt-dev" ;;
  libelf-headers:fedora) printf "elfutils-libelf-devel" ;;
  libelf-headers:opensuse) printf "libelf-devel" ;;
  libelf-headers:arch) printf "libelf" ;;
  libelf-headers:*) printf "libelf-dev" ;;
  node:opensuse) printf "nodejs-default" ;;
  node:*) printf "nodejs" ;;
  npm:opensuse) printf "npm-default" ;;
  docker:debian) printf "docker.io" ;;
  docker:*) printf "docker" ;;
  qemu-system-x86_64:opensuse) printf "qemu-x86" ;;
  qemu-system-x86_64:*) printf "qemu-system-x86" ;;
  qemu-img:debian) printf "qemu-utils" ;;
  qemu-img:opensuse) printf "qemu-tools" ;;
  qemu-img:*) printf "qemu-img" ;;
  virsh:debian) printf "libvirt-clients" ;;
  virsh:arch) printf "libvirt" ;;
  virsh:*) printf "libvirt-client" ;;
  virt-builder:debian | virt-tar-out:debian | virt-make-fs:debian | guestfish:debian) printf "libguestfs-tools" ;;
  virt-builder:arch | virt-tar-out:arch | virt-make-fs:arch | guestfish:arch) printf "libguestfs" ;;
  virt-builder:* | virt-tar-out:* | virt-make-fs:* | guestfish:*) printf "guestfs-tools" ;;
  gcc-or-clang:*) printf "gcc" ;;
  *) printf "%s" "${name}" ;;
  esac
}

# Record a missing distro-packaged dependency under the given tier, de-duplicating
# the package so the guestfish/virt-* family collapses to one install entry.
note_package() {
  local tier="$1" label="$2" package="$3"
  # shellcheck disable=SC2178  # namerefs to per-tier arrays, not string assignments
  local -n cmds="${tier}_commands" pkgs="${tier}_packages"
  cmds+=("${label}")
  local existing
  for existing in ${pkgs[@]+"${pkgs[@]}"}; do
    [[ "${existing}" == "${package}" ]] && return
  done
  pkgs+=("${package}")
}

# Record a missing tool that distros do not package, with its own install command.
note_manual() {
  local tier="$1" label="$2" instruction="$3"
  # shellcheck disable=SC2178  # nameref to a per-tier array, not a string assignment
  local -n cmds="${tier}_commands"
  cmds+=("${label}")
  manual_hints+=("${label}: ${instruction}")
}

require_command() {
  local tier="$1" name="$2" distro="$3"
  command_exists "${name}" || note_package "${tier}" "${name}" "$(package_for "${name}" "${distro}")"
}

require_tool() {
  local tier="$1" name="$2" instruction="$3"
  command_exists "${name}" || note_manual "${tier}" "${name}" "${instruction}"
}

# A header package exposes no binary, so probe pkg-config instead of the PATH.
require_header() {
  local tier="$1" label="$2" module="$3" distro="$4"
  command_exists pkg-config && pkg-config --exists "${module}" 2>/dev/null && return
  note_package "${tier}" "${label}" "$(package_for "${label}" "${distro}")"
}

join_by_comma() {
  local joined="" item
  for item in "$@"; do
    if [[ -z "${joined}" ]]; then
      joined="${item}"
    else
      joined="${joined}, ${item}"
    fi
  done
  printf "%s" "${joined}"
}

print_install_hint() {
  local distro="$1"
  shift
  case "${distro}" in
  fedora) printf "    dnf install %s\n" "$*" ;;
  debian) printf "    apt install %s\n" "$*" ;;
  arch) printf "    pacman -S %s\n" "$*" ;;
  opensuse) printf "    zypper install %s\n" "$*" ;;
  *) printf "    install with your distribution package manager: %s\n" "$*" ;;
  esac
}

report_tier() {
  local heading="$1" tier="$2" distro="$3"
  # shellcheck disable=SC2178  # namerefs to per-tier arrays, not string assignments
  local -n cmds="${tier}_commands" pkgs="${tier}_packages"
  ((${#cmds[@]} == 0)) && return
  printf "\n%s missing: %s\n" "${heading}" "$(join_by_comma "${cmds[@]}")" >&2
  if ((${#pkgs[@]} > 0)); then
    print_install_hint "${distro}" "${pkgs[@]}" >&2
  fi
}

distro="$(load_distro_id)"

# REQUIRED — `uv sync` and the core dev loop fail without these.
require_tool required uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
require_command required pkg-config "${distro}"
require_header required libvirt-headers libvirt "${distro}"

# RECOMMENDED — needed to reproduce the full local CI gate.
require_command recommended git "${distro}"
require_command recommended make "${distro}"
require_tool recommended just "uv tool install rust-just"
require_tool recommended prek "uv tool install prek"
require_command recommended docker "${distro}"
command_exists node || command_exists nodejs ||
  note_package recommended node "$(package_for node "${distro}")"
require_command recommended npm "${distro}"

# FUTURE — live_vm and kernel-build milestones; warn only, never block setup.
for cmd in qemu-system-x86_64 virsh gdb crash virt-builder virt-tar-out \
  virt-make-fs guestfish qemu-img bc flex bison; do
  require_command future "${cmd}" "${distro}"
done
command_exists gcc || command_exists clang ||
  note_package future "gcc or clang" "$(package_for gcc-or-clang "${distro}")"
require_header future libelf-headers libelf "${distro}"

report_tier "Required dependencies" required "${distro}"
report_tier "Recommended dependencies (full local CI)" recommended "${distro}"
report_tier "Future dependencies (live_vm / kernel build)" future "${distro}"

if ((${#manual_hints[@]} > 0)); then
  printf "\nTooling not provided by your distribution:\n" >&2
  printf "    %s\n" "${manual_hints[@]}" >&2
fi

if ((${#required_commands[@]} > 0)); then
  printf "\nInstall the required dependencies from a privileged shell, then rerun: just setup\n" >&2
  exit 1
fi

if ((${#recommended_commands[@]} + ${#future_commands[@]} > 0)); then
  printf "\nRequired dependencies are present; optional items above are not yet needed.\n"
else
  printf "Setup dependencies are present.\n"
fi
