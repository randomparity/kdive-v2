"""Unit tests for the in-process local-libvirt rootfs build plane (M2.4/2, ADR-0092).

These cover the plane's orchestration and provenance contract without libguestfs or qemu: the
slow tools (`virt-builder`, `virt-tar-out`, `virt-make-fs`, `guestfish`) are injected seams the
tests stub. The real libguestfs path is exercised on the operator-run live-stack path. The
acceptance that the produced qcow2 passes `virt-inspector` for the expected layout (whole-disk
ext4, normalized fstab, no crypttab, guest SELinux off) is asserted by recording the guest-side
operations the plane drives — the live path proves the layout, the unit path proves the wiring.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.planes.local_libvirt import LocalLibvirtRootfsBuildPlane, RootfsBuildTools


def _spec(**overrides: object) -> RootfsBuildSpec:
    base: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "fedora-kdive-ready-43",
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("openssh-server", "drgn"),
        "source_image_digest": "sha256:fedora-43-template",
        "capabilities": ("agent", "kdump", "drgn"),
    }
    base.update(overrides)
    return RootfsBuildSpec(**base)  # type: ignore[arg-type]


@dataclass
class _RecordingTools:
    """Stub seams that record the guest-side operations the plane drives."""

    authorized_key: Path
    builder_calls: list[dict[str, object]] = field(default_factory=list)
    repack_calls: list[tuple[Path, Path]] = field(default_factory=list)
    normalize_calls: list[Path] = field(default_factory=list)
    payload: bytes = b"qcow2-bytes"

    def resolve_authorized_key(self) -> Path:
        return self.authorized_key

    def virt_builder(
        self,
        *,
        releasever: str,
        packages: tuple[str, ...],
        authorized_key: Path,
        scratch: Path,
        size: str,
    ) -> None:
        scratch.write_bytes(b"scratch")
        self.builder_calls.append(
            {
                "releasever": releasever,
                "packages": packages,
                "authorized_key": authorized_key,
                "size": size,
            }
        )

    def repack_whole_disk_ext4(self, *, scratch: Path, qcow2: Path, size: str) -> None:
        qcow2.write_bytes(self.payload)
        self.repack_calls.append((scratch, qcow2))

    def normalize_guest(self, qcow2: Path) -> None:
        self.normalize_calls.append(qcow2)


def _plane(tmp_path: Path, tools: _RecordingTools) -> LocalLibvirtRootfsBuildPlane:
    return LocalLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=RootfsBuildTools(
            resolve_authorized_key=tools.resolve_authorized_key,
            virt_builder=tools.virt_builder,
            repack_whole_disk_ext4=tools.repack_whole_disk_ext4,
            normalize_guest=tools.normalize_guest,
        ),
    )


def test_build_produces_qcow2_with_content_digest(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key, payload=b"the-image-bytes")
    out = _plane(tmp_path, tools).build(_spec())

    assert isinstance(out, RootfsBuildOutput)
    assert out.qcow2_path.exists()
    assert out.qcow2_path.read_bytes() == b"the-image-bytes"
    expected = "sha256:" + hashlib.sha256(b"the-image-bytes").hexdigest()
    assert out.digest == expected, "image identity is the qcow2 content digest"


def test_build_records_pinned_provenance(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    out = _plane(tmp_path, tools).build(_spec(releasever="42", packages=("openssh-server",)))

    prov = out.provenance
    assert prov["releasever"] == "42"
    assert prov["packages"] == ["openssh-server"]
    assert prov["source_image_digest"] == "sha256:fedora-43-template"


def test_build_drives_the_layout_stages_in_order(tmp_path: Path) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    out = _plane(tmp_path, tools).build(_spec())

    assert len(tools.builder_calls) == 1, "virt-builder customizes the scratch image once"
    assert tools.builder_calls[0]["packages"] == ("openssh-server", "drgn")
    assert tools.builder_calls[0]["authorized_key"] == key
    assert len(tools.repack_calls) == 1, "repacked to a whole-disk ext4 qcow2 once"
    assert tools.normalize_calls == [out.qcow2_path], "fstab/crypttab/SELinux normalized on output"


def test_build_fails_fast_when_authorized_key_unresolved(tmp_path: Path) -> None:
    key = tmp_path / "missing.pub"  # never created
    tools = _RecordingTools(authorized_key=key)
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, tools).build(_spec())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not tools.builder_calls, "no libguestfs stage runs without a resolvable key"


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", ".hidden", "-leading", "with space"])
def test_build_rejects_a_name_that_would_escape_the_workspace(
    tmp_path: Path, bad_name: str
) -> None:
    key = tmp_path / "id.pub"
    key.write_text("ssh-ed25519 AAAA kdive\n")
    tools = _RecordingTools(authorized_key=key)
    with pytest.raises(CategorizedError) as exc:
        _plane(tmp_path, tools).build(_spec(name=bad_name))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not tools.builder_calls, "an unsafe name is rejected before any libguestfs stage runs"
