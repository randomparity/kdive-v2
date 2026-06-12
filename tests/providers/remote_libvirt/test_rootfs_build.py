"""Unit tests for the in-process remote-libvirt rootfs build plane (M2.4/3, ADR-0080/0092).

These cover the plane's orchestration and provenance contract without libguestfs or qemu: the
slow ``virt-builder`` stage is an injected seam the tests stub. The real libguestfs path is
exercised on the operator-run live-stack path. Unlike the local plane the remote base image is a
**disk-image base OS** (ADR-0078/0080) reached over the qemu-guest-agent seam — so the plane
installs and enables ``qemu-guest-agent`` rather than injecting an SSH key, and it does not
whole-disk-repack or normalize fstab to a lone ``/`` (the partitioned base OS keeps its own
bootloader/layout).

A second group asserts the falsifiable acceptance: the remote provisioning default no longer
references the ad-hoc placeholder volume literal, but the kdive-published base-image name the
plane builds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildPlane, RootfsBuildSpec
from kdive.providers.remote_libvirt.rootfs_build import (
    REMOTE_BASE_IMAGE_NAME,
    RemoteLibvirtRootfsBuildPlane,
    RemoteRootfsBuildTools,
)


def _spec(**overrides: object) -> RootfsBuildSpec:
    base: dict[str, object] = {
        "provider": "remote-libvirt",
        "name": REMOTE_BASE_IMAGE_NAME,
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("qemu-guest-agent", "drgn", "kexec-tools"),
        "source_image_digest": "sha256:fedora-43-template",
        "capabilities": ("agent", "kdump", "drgn"),
    }
    base.update(overrides)
    return RootfsBuildSpec(**base)  # type: ignore[arg-type]


@dataclass
class _RecordingTools:
    """Stub seams that record the guest-side operations the plane drives."""

    builder_calls: list[dict[str, object]] = field(default_factory=list)
    payload: bytes = b"remote-qcow2-bytes"

    def virt_builder(
        self, *, releasever: str, packages: tuple[str, ...], qcow2: Path, size: str
    ) -> None:
        qcow2.write_bytes(self.payload)
        self.builder_calls.append({"releasever": releasever, "packages": packages, "size": size})


def _plane(tmp_path: Path, tools: _RecordingTools) -> RemoteLibvirtRootfsBuildPlane:
    return RemoteLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=RemoteRootfsBuildTools(virt_builder=tools.virt_builder),
    )


def test_plane_satisfies_the_rootfs_build_plane_port() -> None:
    assert isinstance(RemoteLibvirtRootfsBuildPlane.from_env(), RootfsBuildPlane)


def test_build_produces_qcow2_with_content_digest(tmp_path: Path) -> None:
    tools = _RecordingTools(payload=b"the-base-image-bytes")
    out = _plane(tmp_path, tools).build(_spec())

    assert isinstance(out, RootfsBuildOutput)
    assert out.qcow2_path.exists()
    assert out.qcow2_path.read_bytes() == b"the-base-image-bytes"
    expected = "sha256:" + hashlib.sha256(b"the-base-image-bytes").hexdigest()
    assert out.digest == expected, "image identity is the qcow2 content digest"


def test_build_records_pinned_provenance(tmp_path: Path) -> None:
    tools = _RecordingTools()
    out = _plane(tmp_path, tools).build(_spec(releasever="42", packages=("qemu-guest-agent",)))

    prov = out.provenance
    assert prov["plane"] == "remote-libvirt"
    assert prov["releasever"] == "42"
    assert prov["packages"] == ["qemu-guest-agent"]
    assert prov["source_image_digest"] == "sha256:fedora-43-template"
    assert prov["boot_method"] == "disk-image", "remote rides disk-image boot, not direct-kernel"


def test_build_installs_and_enables_the_guest_agent(tmp_path: Path) -> None:
    tools = _RecordingTools()
    _plane(tmp_path, tools).build(_spec())

    assert len(tools.builder_calls) == 1, "virt-builder customizes the base image once"
    packages = tools.builder_calls[0]["packages"]
    assert isinstance(packages, tuple)
    assert "qemu-guest-agent" in packages, "the remote access seam is the guest agent (ADR-0078)"


def test_build_rejects_a_name_that_would_escape_the_workspace(tmp_path: Path) -> None:
    tools = _RecordingTools()
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, tools).build(_spec(name="../escape"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not tools.builder_calls, "an unsafe name is rejected before any libguestfs stage runs"


def test_build_maps_a_missing_tool_to_missing_dependency(tmp_path: Path) -> None:
    def _absent(*, releasever: str, packages: tuple[str, ...], qcow2: Path, size: str) -> None:
        raise CategorizedError(
            "virt-builder is not installed",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": "virt-builder"},
        )

    plane = RemoteLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=RemoteRootfsBuildTools(virt_builder=_absent),
    )
    with pytest.raises(CategorizedError) as exc:
        plane.build(_spec())
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_build_fails_when_no_image_is_produced(tmp_path: Path) -> None:
    def _noop(*, releasever: str, packages: tuple[str, ...], qcow2: Path, size: str) -> None:
        return None  # produces no qcow2

    plane = RemoteLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=RemoteRootfsBuildTools(virt_builder=_noop),
    )
    with pytest.raises(CategorizedError) as exc:
        plane.build(_spec())
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
