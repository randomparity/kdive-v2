"""Curated catalog of non-registry ``KDIVE_*`` environment variables.

The 48 runtime settings the processes read flow through the config registry
(:func:`kdive.config.all_settings`) and are auto-documented by
``scripts/gen_config_reference.py``. A second class of ``KDIVE_*`` variables is read **outside**
the registry — by the gated test suites, the operator setup/live-stack shell scripts, and the
in-guest capture/install helpers. Those cannot go through ``kdive.config`` (a bash helper has no
Python import; a test fixture is not a process setting), so they are catalogued here by hand.

This module is the single source of truth for that second class. The config-reference generator
renders it into a second section of ``docs/guide/reference/config.md``, and
``scripts/check_env_documented.py`` treats every name here as documented — so a new non-registry
variable fails CI until it is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EnvScope = Literal["test", "script", "guest"]


@dataclass(frozen=True, slots=True)
class ExternalEnvVar:
    """A ``KDIVE_*`` variable read outside the config registry.

    Attributes:
        name: The environment variable name (``KDIVE_...``).
        scope: Where it is read — ``test`` (gated suites), ``script`` (operator shell scripts),
            or ``guest`` (in-guest capture/install helpers).
        default: The fallback when unset, or ``None`` when unset means "skip / required".
        help: One line describing what reads it and what it controls.
    """

    name: str
    scope: EnvScope
    default: str | None
    help: str


EXTERNAL_ENV_VARS: tuple[ExternalEnvVar, ...] = (
    # --- test-only (gated suites) ---------------------------------------------------------
    ExternalEnvVar(
        "KDIVE_GUEST_IMAGE",
        "test",
        None,
        "Path to the operator-built local-libvirt guest rootfs qcow2 the live_stack spine boots; "
        "unset → the live_stack suite skips.",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_BUILD_CONFIG",
        "test",
        None,
        "Path or file:// URL to a kernel .config (kdump + debuginfo) for the live_vm real-make "
        "build-id test; unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_SSH_TARGET",
        "test",
        None,
        "SSH target gating the criterion-5 live_stack tier; unset → the live_stack suite skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_SYSTEM_ID",
        "test",
        None,
        "System id of a pre-provisioned live VM for the gated local-libvirt install test.",
    ),
    ExternalEnvVar(
        "KDIVE_REQUIRE_DOCKER",
        "test",
        "0",
        "Set to 1 to fail (not skip) the disposable-Postgres/MinIO fixtures when Docker is absent.",
    ),
    ExternalEnvVar(
        "KDIVE_IMAGE",
        "test",
        None,
        "Container image ref under test for the image smoke test; unset → the smoke test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_BASE_URL",
        "test",
        None,
        "Base URL of a running kdive server for the live_stack HTTP tier; unset → that tier skips.",
    ),
    ExternalEnvVar(
        "KDIVE_ARTIFACT_DIR",
        "test",
        None,
        "Directory the live_stack spine writes run artifacts to (default: an out-of-tree "
        "temp dir).",
    ),
    ExternalEnvVar(
        "KDIVE_OIDC_CLIENT_ID",
        "test",
        "kdive-test",
        "OIDC client id the live_stack harness presents to the mock issuer.",
    ),
    ExternalEnvVar(
        "KDIVE_SEAM_DOMAIN",
        "test",
        None,
        "libvirt domain name for the in-target guest-agent seam live test.",
    ),
    ExternalEnvVar(
        "KDIVE_SEAM_URI",
        "test",
        None,
        "libvirt connection URI for the in-target guest-agent seam live test.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_BASE_IMAGE_VOLUME",
        "test",
        None,
        "Name of the prebuilt remote-libvirt base-image storage volume for the remote live_stack "
        "test; unset → that test skips.",
    ),
    # --- operator shell scripts -----------------------------------------------------------
    ExternalEnvVar(
        "KDIVE_KVM_NODE",
        "script",
        "/dev/kvm",
        "KVM device node `check-local-libvirt.sh` probes for hardware virtualization.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_SSH_PORT",
        "script",
        "22",
        "SSH port `check-remote-libvirt.sh` connects on.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_PKI_DIR",
        "script",
        "/etc/pki/libvirt",
        "TLS PKI directory `check-remote-libvirt.sh` validates.",
    ),
    ExternalEnvVar(
        "KDIVE_GUEST_HELPERS_DIR",
        "script",
        "deploy/remote-libvirt-guest-helpers",
        "Guest-helper source directory `check-remote-libvirt.sh` inspects.",
    ),
    ExternalEnvVar(
        "KDIVE_OS_RELEASE",
        "script",
        "/etc/os-release",
        "os-release file `check-setup-deps.sh` reads to detect the host distro.",
    ),
    ExternalEnvVar(
        "KDIVE_KERNEL_REPO",
        "script",
        "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
        "Kernel git remote `fetch-kernel-tree.sh` clones.",
    ),
    ExternalEnvVar(
        "KDIVE_KERNEL_REF",
        "script",
        "v6.9",
        "Kernel ref (tag/branch/sha) `fetch-kernel-tree.sh` checks out.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_SSH_PORT",
        "script",
        "22",
        "SSH port `check-ssh-reachable.sh` probes.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_PID_FILE",
        "script",
        "<repo>/.live-stack.pid",
        "PID file the live-stack `start.sh`/`stop.sh` scripts manage.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_LOG_DIR",
        "script",
        "<repo>/.live-stack-logs",
        "Log directory the live-stack `start.sh` script writes process logs to.",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_NAMESPACE",
        "script",
        "kdive-demo",
        "Release namespace `demo-token.sh` targets when minting a bundled-demo bearer token.",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_FULLNAME",
        "script",
        "kdive-kdive",
        "Chart fullname (`<release>-kdive`) `demo-token.sh` uses to address the server/oidc pods.",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_CONTEXT",
        "script",
        None,
        "kube context `demo-token.sh` uses (unset → the current context).",
    ),
    # --- in-guest capture/install helpers -------------------------------------------------
    ExternalEnvVar(
        "KDIVE_VMCORE_PATH",
        "guest",
        "/var/crash/*/vmcore",
        "Override the vmcore path `kdive-capture-vmcore` reads (default: the kdump-utils path).",
    ),
    ExternalEnvVar(
        "KDIVE_DMESG_CAP_BYTES",
        "guest",
        "1048576",
        "Byte cap on the inline dmesg `kdive-capture-vmcore` emits (default 1 MiB).",
    ),
    ExternalEnvVar(
        "KDIVE_TITLE",
        "guest",
        "kdive",
        "grub menu title the `kdive-install-kernel` helper assigns the kdive boot slot.",
    ),
)


def external_env_names() -> frozenset[str]:
    """Return the set of documented non-registry ``KDIVE_*`` variable names."""
    return frozenset(var.name for var in EXTERNAL_ENV_VARS)
