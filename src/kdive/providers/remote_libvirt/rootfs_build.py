"""The in-process remote-libvirt rootfs build plane (M2.4/3, ADR-0080, ADR-0092).

`RemoteLibvirtRootfsBuildPlane` builds the **provisioning disk-image** the remote-libvirt
provider boots (ADR-0080's ``base_image_volume``), replacing the placeholder base image the
provider shipped with a real built-and-published image whose identity is its qcow2 **content
digest** and whose pinned inputs are recorded as **provenance** (:class:`RootfsBuildOutput`).

It differs from the local-libvirt plane on two ADR-bound points:

1. **Access seam.** The remote provider reaches the guest through the **qemu-guest-agent**
   over ``qemu+tls://`` (ADR-0078/0079), not SSH. So the plane installs and enables
   ``qemu-guest-agent`` (with the spec's packages — drgn, kdump tooling) instead of injecting a
   kdive-managed SSH key. The base image's full content obligations (matching vmlinux/debuginfo,
   crashkernel-capable kernel) remain the operator's contract, recorded in provenance.
2. **Boot model.** Remote boots a **disk-image base OS** (ADR-0080) with its own bootloader and
   partition layout, not local's no-partition-table whole-disk ext4 for ``root=/dev/vda``
   direct-kernel boot (ADR-0030). So there is **no** ``virt-tar-out`` / ``virt-make-fs`` repack
   and **no** fstab-to-lone-``/`` normalization — ``virt-builder`` emits the bootable image
   directly.

The slow ``virt-builder`` stage is an **injected seam** (:class:`RemoteRootfsBuildTools`) that
defaults to the real implementation, so unit tests cover the orchestration/provenance contract
without libguestfs or qemu; the real path is exercised on the operator-run live-stack path.
``build()`` is synchronous — the worker offloads the whole call via ``asyncio.to_thread``
(ADR-0092). Bit-reproducible rebuilds are an explicit non-goal (ADR-0092): the falsifiable
contract is the recorded provenance, and the image identity is the output qcow2 content digest.
"""

from __future__ import annotations

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

# The kdive-published remote provisioning base image's catalog name (ADR-0092). The remote
# provisioning profile's `base_image_volume` derives the operator-staged volume name from it
# (e.g. `<name>.qcow2`), replacing the ad-hoc placeholder literal the ADR-0080 plane shipped.
# The image's verifiable identity is its qcow2 content digest, not this name.
REMOTE_BASE_IMAGE_NAME = "fedora-kdive-remote-base-43"

# The guest-agent package is the remote access seam (ADR-0078/0079); the plane always installs
# and enables it regardless of the spec's package set, so the built image satisfies the contract.
_GUEST_AGENT_PACKAGE = "qemu-guest-agent"

_DEFAULT_WORKSPACE = "/var/lib/kdive/build/images"
_DEFAULT_IMAGE_SIZE = "10G"
_VIRT_BUILDER_TIMEOUT_S = 30 * 60


def _run(argv: list[str], *, stage: str, timeout_s: int) -> None:
    """Run a fixed-argv libguestfs tool, mapping failure onto a categorized error."""
    run_guestfs_tool(
        argv,
        stage=stage,
        timeout_s=timeout_s,
        missing_message=f"{argv[0]} is not installed; cannot build the remote base image",
    )


def _guest_agent_packages(packages: tuple[str, ...]) -> tuple[str, ...]:
    """The install set with the guest-agent package present exactly once, order preserved."""
    if _GUEST_AGENT_PACKAGE in packages:
        return packages
    return (_GUEST_AGENT_PACKAGE, *packages)


def _real_virt_builder(
    *, releasever: str, packages: tuple[str, ...], qcow2: Path, size: str
) -> None:
    """Customize a bootable Fedora disk-image base OS: guest agent + packages, agent enabled."""
    argv = [
        "virt-builder",
        f"fedora-{releasever}",
        "--format",
        "qcow2",
        "--size",
        size,
        "--output",
        str(qcow2),
        "--install",
        ",".join(_guest_agent_packages(packages)),
        "--run-command",
        f"systemctl enable {_GUEST_AGENT_PACKAGE}.service",
    ]
    _run(argv, stage="virt-builder", timeout_s=_VIRT_BUILDER_TIMEOUT_S)


type VirtBuilder = Callable[..., None]


@dataclass(frozen=True, slots=True)
class RemoteRootfsBuildTools:
    """The injectable build seam; defaults to the real libguestfs implementation."""

    virt_builder: VirtBuilder = _real_virt_builder


class RemoteLibvirtRootfsBuildPlane:
    """The realized remote-libvirt :class:`~kdive.images.planes.base.RootfsBuildPlane`."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        size: str = _DEFAULT_IMAGE_SIZE,
        tools: RemoteRootfsBuildTools | None = None,
    ) -> None:
        self._workspace = workspace or Path(_DEFAULT_WORKSPACE)
        self._size = size
        self._tools = tools or RemoteRootfsBuildTools()

    @classmethod
    def from_env(cls) -> RemoteLibvirtRootfsBuildPlane:
        """Build with the real libguestfs seam; does not run any tool or touch the network."""
        return cls()

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the remote provisioning disk-image for ``spec``; record pinned-input provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a name that would escape the workspace,
                ``MISSING_DEPENDENCY`` for absent libguestfs tooling, ``PROVISIONING_FAILURE``
                for a build-stage failure or an absent output image, or ``INFRASTRUCTURE_FAILURE``
                for a non-``FileNotFound`` launch error.
        """
        validate_image_name(spec.name)
        with build_workspace(self._workspace, prefix="remote-build-") as work_dir:
            scratch = work_dir / f"{spec.name}.qcow2"
            self._tools.virt_builder(
                releasever=spec.releasever,
                packages=spec.packages,
                qcow2=scratch,
                size=self._size,
            )
            if not scratch.is_file():
                raise CategorizedError(
                    "virt-builder reported success but produced no image",
                    category=ErrorCategory.PROVISIONING_FAILURE,
                    details={"stage": "virt-builder"},
                )
            qcow2 = publish_qcow2(self._workspace, image_name=spec.name, scratch=scratch)
        digest = digest_file(qcow2)
        return RootfsBuildOutput(
            qcow2_path=qcow2, digest=digest, provenance=_provenance(spec, size=self._size)
        )


def _provenance(spec: RootfsBuildSpec, *, size: str) -> dict[str, object]:
    """Record the pinned inputs and build args that produced the image (falsifiable contract).

    ``source_image_digest`` is the caller-declared base/template pin recorded as requested — the
    plane does not re-fetch and checksum the virt-builder template, so it names what was *asked
    for*, not a plane-verified hash. The image's verifiable identity is the output qcow2 content
    digest (:func:`kdive.images.planes._build_common.digest_file`), per ADR-0092.
    """
    return {
        "plane": "remote-libvirt",
        "boot_method": "disk-image",
        "releasever": spec.releasever,
        "packages": list(_guest_agent_packages(spec.packages)),
        "source_image_digest": spec.source_image_digest,
        "capabilities": list(spec.capabilities),
        "arch": spec.arch,
        "image_size": size,
        "guest_access_seam": "qemu-guest-agent",
    }


__all__ = [
    "REMOTE_BASE_IMAGE_NAME",
    "RemoteLibvirtRootfsBuildPlane",
    "RemoteRootfsBuildTools",
]
