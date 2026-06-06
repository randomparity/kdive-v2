"""Tests for the local-libvirt Build plane (ADR-0027)."""

from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.local_libvirt.build import (
    LocalLibvirtBuild,
    parse_gnu_build_id,
)
from kdive.store.objectstore import StoredArtifact

_RUN = UUID("22222222-2222-2222-2222-222222222222")
_TENANT = "proj"

_VALID_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config_ref": "file:///configs/x86_64-kdump.config",
    "patch_ref": None,
}

# A .config that satisfies both preflight checks (kdump + debuginfo).
_GOOD_CONFIG = "\n".join(
    [
        "CONFIG_CRASH_DUMP=y",
        "CONFIG_DEBUG_INFO=y",
        "CONFIG_DEBUG_INFO_DWARF5=y",
    ]
)

_NT_GNU_BUILD_ID = 3


def _gnu_build_id_note(build_id: bytes) -> bytes:
    """Build a little-endian ELF note section carrying ``build_id`` as the GNU build-id.

    Layout: namesz, descsz, type (4-byte LE each); name ``"GNU\\0"`` (4 bytes, already
    aligned); desc = the build-id bytes, padded to 4-byte alignment.
    """
    name = b"GNU\x00"
    desc = build_id
    pad = (-len(desc)) % 4
    header = struct.pack("<III", len(name), len(desc), _NT_GNU_BUILD_ID)
    return header + name + desc + b"\x00" * pad


def _profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(_VALID_PROFILE)
    assert isinstance(profile, ServerBuildProfile)
    return profile


@dataclass
class _FakeStore:
    """Records puts; returns a StoredArtifact echoing the key (no real S3)."""

    puts: list[tuple[str, str, str, Sensitivity]] = field(default_factory=list)
    fail_on: str | None = None  # raise INFRASTRUCTURE_FAILURE on this name

    def put_artifact(
        self,
        tenant: str,
        kind: str,
        object_id: str,
        name: str,
        *,
        data: bytes,
        sensitivity: Sensitivity,
        retention_class: str,
    ) -> StoredArtifact:
        if self.fail_on == name:
            raise CategorizedError(
                "synthetic put failure",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        key = f"{tenant}/{kind}/{object_id}/{name}"
        self.puts.append((key, name, kind, sensitivity))
        return StoredArtifact(key, "etag-" + name, sensitivity, retention_class)


@dataclass
class _Seams:
    """Injected slow-op seams; default to canned successful outputs."""

    config_text: str = _GOOD_CONFIG
    build_id: bytes = b"\xab\xcd\xef\x01\x23\x45\x67\x89"
    make_returncode: int = 0
    make_calls: int = 0
    checkout_calls: int = 0

    def checkout(self, run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        self.checkout_calls += 1

    def read_config(self, workspace: Path) -> str:
        return self.config_text

    def run_make(self, workspace: Path) -> int:
        self.make_calls += 1
        return self.make_returncode

    def read_kernel_image(self, workspace: Path) -> bytes:
        return b"bzImage-bytes"

    def read_vmlinux(self, workspace: Path) -> bytes:
        return b"vmlinux-bytes"

    def read_build_id(self, workspace: Path) -> str:
        return self.build_id.hex()


def _builder(store: _FakeStore, seams: _Seams, tmp_path: Path) -> LocalLibvirtBuild:
    return LocalLibvirtBuild(
        tenant=_TENANT,
        workspace_root=tmp_path,
        store_factory=lambda: store,
        checkout=seams.checkout,
        read_config=seams.read_config,
        run_make=seams.run_make,
        read_kernel_image=seams.read_kernel_image,
        read_vmlinux=seams.read_vmlinux,
        read_build_id=seams.read_build_id,
    )


# --- build-id note parser ------------------------------------------------------------


def test_parse_gnu_build_id_extracts_hex() -> None:
    build_id = b"\xde\xad\xbe\xef\x00\x11\x22\x33\x44\x55"
    note = _gnu_build_id_note(build_id)
    assert parse_gnu_build_id(note) == build_id.hex()


def test_parse_gnu_build_id_finds_note_after_padding() -> None:
    # A leading note of a different type, then the build-id note: the parser walks past it.
    other = struct.pack("<III", 4, 4, 1) + b"GNU\x00" + b"\x00\x00\x00\x00"
    build_id = b"\x01\x02\x03\x04"
    note = other + _gnu_build_id_note(build_id)
    assert parse_gnu_build_id(note) == build_id.hex()


def test_parse_gnu_build_id_absent_raises() -> None:
    with pytest.raises(CategorizedError) as caught:
        parse_gnu_build_id(b"\x00" * 16)
    assert caught.value.category is ErrorCategory.BUILD_FAILURE


def test_parse_gnu_build_id_truncated_note_raises_not_loops() -> None:
    # A note header claiming a descsz that runs past the buffer is corrupt: the parser
    # stops (no build-id) rather than reading out of bounds or looping.
    truncated = struct.pack("<III", 4, 999, _NT_GNU_BUILD_ID) + b"GNU\x00" + b"\x01\x02"
    with pytest.raises(CategorizedError) as caught:
        parse_gnu_build_id(truncated)
    assert caught.value.category is ErrorCategory.BUILD_FAILURE


def test_parse_gnu_build_id_zero_length_note_terminates() -> None:
    # A run of zero-length non-build-id notes must advance the cursor and terminate.
    zero_notes = (struct.pack("<III", 0, 0, 1)) * 4
    with pytest.raises(CategorizedError):
        parse_gnu_build_id(zero_notes)


# --- build() happy path --------------------------------------------------------------


def test_build_returns_two_refs_and_build_id(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()
    out = _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert out.kernel_ref == f"{_TENANT}/runs/{_RUN}/kernel"
    assert out.debuginfo_ref == f"{_TENANT}/runs/{_RUN}/vmlinux"
    assert out.build_id == seams.build_id.hex()
    assert seams.make_calls == 1


def test_build_stores_both_artifacts_sensitive(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()
    _builder(store, seams, tmp_path).build(_RUN, _profile())

    names = {name for _, name, _, _ in store.puts}
    assert names == {"kernel", "vmlinux"}
    assert all(sens is Sensitivity.SENSITIVE for _, _, _, sens in store.puts)
    assert all(kind == "runs" for _, _, kind, _ in store.puts)


# --- config preflight ----------------------------------------------------------------


@pytest.mark.parametrize(
    "config_text",
    [
        "CONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF5=y",  # no kdump
        "CONFIG_CRASH_DUMP=y",  # no debuginfo
        "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO=y",  # debuginfo but no DWARF/BTF
        "# CONFIG_CRASH_DUMP is not set\nCONFIG_DEBUG_INFO_DWARF5=y",  # explicitly unset
    ],
)
def test_build_rejects_config_missing_prereq_before_make(tmp_path: Path, config_text: str) -> None:
    store, seams = _FakeStore(), _Seams(config_text=config_text)
    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert seams.make_calls == 0  # preflight is before make
    assert store.puts == []  # nothing stored on a config rejection


# --- make / store failures -----------------------------------------------------------


def test_build_nonzero_make_is_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(make_returncode=2)
    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())
    assert caught.value.category is ErrorCategory.BUILD_FAILURE


def test_build_store_failure_propagates_infrastructure(tmp_path: Path) -> None:
    store, seams = _FakeStore(fail_on="kernel"), _Seams()
    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


# --- from_env does not connect/spawn -------------------------------------------------


def test_from_env_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive")
    monkeypatch.setenv("KDIVE_BUILD_WORKSPACE", "/tmp/kdive-build")
    monkeypatch.setenv("KDIVE_KERNEL_SRC", "/tmp/kernel-src")

    def _no_make(*_: object, **__: object) -> object:
        raise AssertionError("from_env must not run make")

    monkeypatch.setattr(subprocess, "run", _no_make)
    builder = LocalLibvirtBuild.from_env()  # building must not spawn make or connect S3
    assert isinstance(builder, LocalLibvirtBuild)


# --- live_vm real-make build ---------------------------------------------------------


@pytest.mark.live_vm
def test_live_vm_real_make_build_id_matches_readelf() -> None:  # pragma: no cover - live_vm
    import os
    import shutil

    src = os.environ.get("KDIVE_KERNEL_SRC")
    if not src or not shutil.which("readelf"):
        pytest.skip("KDIVE_KERNEL_SRC or readelf unavailable")
    # The real build runs against the operator-provided warm tree; the assertion that the
    # extracted build-id equals `readelf -n vmlinux` lives here so extraction is tested
    # against a real ELF. Implemented as part of the live_vm gated suite (#18).
    raise NotImplementedError("live_vm real-make harness wired by the live_vm runner")
