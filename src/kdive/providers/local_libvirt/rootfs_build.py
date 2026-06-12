"""The in-process local-libvirt rootfs build plane (M2.4/2, ADR-0052, ADR-0092).

`LocalLibvirtRootfsBuildPlane` orchestrates the same unprivileged libguestfs stages the deleted
bash rootfs builder ran, but in-process and with **pinned-input provenance** recorded into the
:class:`RootfsBuildOutput`:

1. resolve the kdive-managed SSH public key (ADR-0052 — the single source of truth shared with
   the connect-time ``ssh -i`` identity);
2. ``virt-builder`` customizes a base scratch image: install ``openssh-server`` + the spec's
   packages, enable ``sshd``, inject the authorized key, and install a ``kdive-ready`` oneshot
   unit that echoes the readiness marker to ``/dev/ttyS0`` on boot;
3. ``virt-tar-out`` + ``virt-make-fs --type=ext4 --format=qcow2`` repack the root tree into a
   **no-partition-table whole-disk ext4 qcow2** — the only layout the direct-kernel boot
   provider mounts (``root=/dev/vda``, no initramfs, ADR-0030);
4. ``guestfish`` normalizes the inherited mount config to a lone ``/`` fstab entry, removes
   ``/etc/crypttab``, and disables guest-internal SELinux (so the host-written authorized_keys is
   read without a relabel and the first boot does not relabel+reboot).

The slow libguestfs tools are **injected seams** (:class:`RootfsBuildTools`) that default to the
real implementations, so unit tests cover the orchestration/provenance contract without
libguestfs or qemu; the real path is exercised on the operator-run live-stack path. ``build()``
is synchronous — the worker offloads the whole call via ``asyncio.to_thread`` (ADR-0092).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes._build_common import (
    build_workspace,
    digest_file,
    publish_qcow2,
    run_guestfs_tool,
    validate_image_name,
)
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.prereqs.managed_ssh_key import (
    ManagedKeyError,
    ensure_managed_keypair,
    managed_public_key_path,
)

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_DEFAULT_IMAGE_SIZE = "6G"
_READINESS_MARKER = "kdive-ready"
_VIRT_BUILDER_TIMEOUT_S = 30 * 60
_REPACK_TIMEOUT_S = 30 * 60
_GUESTFISH_TIMEOUT_S = 5 * 60

_READINESS_UNIT = f"""[Unit]
Description=Signal kdive serial readiness
After=dev-ttyS0.device
Wants=dev-ttyS0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo {_READINESS_MARKER} > /dev/ttyS0'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
_FSTAB = "/dev/vda / ext4 defaults 0 1\n"
_SELINUX_CONFIG = "SELINUX=disabled\nSELINUXTYPE=targeted\n"


def _resolve_managed_public_key() -> Path:
    """Resolve the kdive-managed SSH public key, generating the keypair if absent (ADR-0052)."""
    try:
        ensure_managed_keypair()
        return managed_public_key_path()
    except ManagedKeyError as exc:
        raise CategorizedError(
            "could not resolve the kdive-managed SSH public key to install into the rootfs",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"error": type(exc).__name__},
        ) from exc


def _run(argv: list[str], *, stage: str, timeout_s: int) -> None:
    """Run a fixed-argv libguestfs tool, mapping failure onto a categorized error."""
    run_guestfs_tool(
        argv,
        stage=stage,
        timeout_s=timeout_s,
        missing_message=f"{argv[0]} is not installed; cannot build the rootfs image",
    )


def _real_virt_builder(
    *, releasever: str, packages: tuple[str, ...], authorized_key: Path, scratch: Path, size: str
) -> None:
    """Customize a base scratch image: sshd + key + the kdive-ready marker unit + packages."""
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as unit:
        unit.write(_READINESS_UNIT)
        unit_path = Path(unit.name)
    try:
        argv = [
            "virt-builder",
            f"fedora-{releasever}",
            "--format",
            "qcow2",
            "--size",
            size,
            "--output",
            str(scratch),
            "--install",
            "openssh-server",
            "--run-command",
            "systemctl enable sshd.service",
        ]
        if packages:
            argv += ["--install", ",".join(packages)]
        argv += [
            "--ssh-inject",
            f"root:file:{authorized_key}",
            "--upload",
            f"{unit_path}:/etc/systemd/system/{_READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {_READINESS_MARKER}.service",
        ]
        _run(argv, stage="virt-builder", timeout_s=_VIRT_BUILDER_TIMEOUT_S)
    finally:
        unit_path.unlink(missing_ok=True)


def _real_repack_whole_disk_ext4(*, scratch: Path, qcow2: Path, size: str) -> None:
    """Repack the customized root tree into a no-partition-table whole-disk ext4 qcow2."""
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        tar_path = Path(handle.name)
    try:
        _run(
            ["virt-tar-out", "-a", str(scratch), "/", str(tar_path)],
            stage="virt-tar-out",
            timeout_s=_REPACK_TIMEOUT_S,
        )
        _run(
            [
                "virt-make-fs",
                "--type=ext4",
                "--format=qcow2",
                f"--size={size}",
                str(tar_path),
                str(qcow2),
            ],
            stage="virt-make-fs",
            timeout_s=_REPACK_TIMEOUT_S,
        )
    finally:
        tar_path.unlink(missing_ok=True)


def _real_normalize_guest(qcow2: Path) -> None:
    """Normalize fstab to a lone ``/``, remove crypttab, and disable guest SELinux via guestfish."""
    with tempfile.NamedTemporaryFile("w", suffix=".fstab", delete=False) as fstab_handle:
        fstab_handle.write(_FSTAB)
        fstab_path = Path(fstab_handle.name)
    with tempfile.NamedTemporaryFile("w", suffix=".selinux", delete=False) as selinux_handle:
        selinux_handle.write(_SELINUX_CONFIG)
        selinux_path = Path(selinux_handle.name)
    script = (
        f"upload {fstab_path} /etc/fstab\n"
        f"upload {selinux_path} /etc/selinux/config\n"
        "rm-f /etc/crypttab\n"
    )
    try:
        _run_guestfish(qcow2, script)
    finally:
        fstab_path.unlink(missing_ok=True)
        selinux_path.unlink(missing_ok=True)


def _run_guestfish(qcow2: Path, script: str) -> None:
    run_guestfs_tool(
        ["guestfish", "--rw", "-a", str(qcow2), "-i"],
        stage="guestfish",
        timeout_s=_GUESTFISH_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot normalize the rootfs image",
        failure_message="guestfish normalization failed",
        input_text=script,
    )


type ResolveAuthorizedKey = Callable[[], Path]
type VirtBuilder = Callable[..., None]
type RepackWholeDiskExt4 = Callable[..., None]
type NormalizeGuest = Callable[[Path], None]


@dataclass(frozen=True, slots=True)
class RootfsBuildTools:
    """The injectable build seams; default to the real libguestfs implementations."""

    resolve_authorized_key: ResolveAuthorizedKey = _resolve_managed_public_key
    virt_builder: VirtBuilder = _real_virt_builder
    repack_whole_disk_ext4: RepackWholeDiskExt4 = _real_repack_whole_disk_ext4
    normalize_guest: NormalizeGuest = _real_normalize_guest


class LocalLibvirtRootfsBuildPlane:
    """The realized local-libvirt :class:`~kdive.images.planes.base.RootfsBuildPlane`."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        size: str = _DEFAULT_IMAGE_SIZE,
        tools: RootfsBuildTools | None = None,
    ) -> None:
        self._workspace = workspace or Path(_DEFAULT_WORKSPACE)
        self._size = size
        self._tools = tools or RootfsBuildTools()

    @classmethod
    def from_env(cls) -> LocalLibvirtRootfsBuildPlane:
        """Build with the real libguestfs seams; does not run any tool or touch the network."""
        return cls()

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the kdive-ready rootfs qcow2 for ``spec``; record pinned-input provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unresolvable authorized key,
                ``MISSING_DEPENDENCY`` for absent libguestfs tooling, or ``PROVISIONING_FAILURE``
                for a build-stage failure.
        """
        validate_image_name(spec.name)
        authorized_key = self._tools.resolve_authorized_key()
        if not authorized_key.is_file():
            raise CategorizedError(
                "resolved SSH public key is not a readable file; cannot build the rootfs image",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"authorized_key": str(authorized_key)},
            )
        with build_workspace(self._workspace, prefix="rootfs-build-") as work_dir:
            scratch = work_dir / "scratch.qcow2"
            self._tools.virt_builder(
                releasever=spec.releasever,
                packages=spec.packages,
                authorized_key=authorized_key,
                scratch=scratch,
                size=self._size,
            )
            staged = work_dir / f"{spec.name}.qcow2"
            self._tools.repack_whole_disk_ext4(scratch=scratch, qcow2=staged, size=self._size)
            self._tools.normalize_guest(staged)
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=staged)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2,
            digest=digest,
            provenance=_provenance(spec, size=self._size, authorized_key=authorized_key),
        )


def _provenance(spec: RootfsBuildSpec, *, size: str, authorized_key: Path) -> dict[str, object]:
    """Record the pinned inputs and build args that produced the image (falsifiable contract).

    ``source_image_digest`` is the caller-declared base/template pin recorded as requested — the
    plane does not re-fetch and checksum the virt-builder template, so it names what was *asked
    for*, not a plane-verified hash. The image's verifiable identity is the output qcow2 content
    digest (:func:`kdive.images.planes._build_common.digest_file`), per ADR-0092.
    """
    return {
        "plane": "local-libvirt",
        "releasever": spec.releasever,
        "packages": list(spec.packages),
        "source_image_digest": spec.source_image_digest,
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "authorized_key_name": authorized_key.name,
        "readiness_marker": _READINESS_MARKER,
        "layout": "whole-disk-ext4-qcow2",
        "guest_selinux": "disabled",
    }
