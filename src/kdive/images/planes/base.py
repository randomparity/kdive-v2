"""The `RootfsBuildPlane` port: the provider-agnostic rootfs-image build contract (ADR-0092).

A `RootfsBuildPlane` turns a declarative :class:`RootfsBuildSpec` into a built, object-ready
qcow2 plus **recorded provenance** (:class:`RootfsBuildOutput`). The image's identity is the
content digest of the produced qcow2 — a rootfs image has no kernel ``build_id`` (a vmlinux
ELF-note), and a ``virt-builder``-customized image is not bit-reproducible (mirror drift,
embedded timestamps, filesystem ordering), so **bit-reproducible rebuilds are an explicit
non-goal**. The falsifiable contract is the recorded provenance: the output names exactly the
pinned inputs (releasever, package set, source-image digest) that produced its image.

This module is the public seam every provider implements (local-libvirt in this milestone,
remote-libvirt in #284) and the publish/`IMAGE_BUILD` layers consume. Implementations inherit
nothing — they satisfy the :class:`RootfsBuildPlane` ``Protocol`` structurally — so the
dataclass field sets and the ``build(spec) -> RootfsBuildOutput`` signature here are a stable
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RootfsBuildSpec:
    """Declarative inputs for a rootfs build (the pinned inputs become provenance).

    Attributes:
        provider: The provider whose plane builds the image (e.g. ``"local-libvirt"``).
        name: The catalog image name (e.g. ``"fedora-kdive-ready-43"``).
        arch: The target architecture (e.g. ``"x86_64"``).
        releasever: The base-OS release the image is built from (e.g. ``"43"``).
        packages: The package set installed into the guest, in install order.
        source_image_digest: A digest pinning the base/template image the build customizes.
        capabilities: The guest-contract tags the image is expected to satisfy (agent, kdump,
            drgn, allowlisted helpers).
    """

    provider: str
    name: str
    arch: str
    releasever: str
    packages: tuple[str, ...]
    source_image_digest: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootfsBuildOutput:
    """The product of a rootfs build.

    Attributes:
        qcow2_path: Local path to the produced qcow2 (object-ready; publish writes it to the
            object store and content-addresses it by :attr:`digest`).
        digest: The content digest of the produced qcow2 (``"sha256:<hex>"``) — the image
            identity. Distinct from a kernel ``build_id``; a rootfs image has none.
        provenance: The pinned inputs and build args that produced the image, JSONB-serializable
            for the catalog row's ``provenance`` column (the falsifiable contract).
    """

    qcow2_path: Path
    digest: str
    provenance: dict[str, object]


@runtime_checkable
class RootfsBuildPlane(Protocol):
    """A provider's in-process rootfs build plane.

    The single operation is a synchronous, environment-bound build (libguestfs/qemu, minutes);
    callers on the worker offload it via ``asyncio.to_thread`` so it never stalls the event loop.
    """

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        """Build the image declared by ``spec`` and return its path, digest, and provenance.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid/unresolvable inputs,
                ``MISSING_DEPENDENCY`` for absent build tooling, or ``PROVISIONING_FAILURE`` for
                a build-stage failure.
        """
        ...
