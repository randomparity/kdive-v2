"""Tests for the local-libvirt Build plane (ADR-0027)."""

from __future__ import annotations

import hashlib
import shutil
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
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.references import LocalComponentRef
from kdive.providers.build_validation import parse_gnu_build_id
from kdive.providers.local_libvirt import build as build_module
from kdive.providers.local_libvirt.build import (
    LocalLibvirtBuild,
    _apply_patch,
    _real_read_config,
    _real_read_kernel_image,
    _resolve_local_ref,
    _stage_config,
    _sync_tree,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("22222222-2222-2222-2222-222222222222")
_TENANT = "proj"

_VALID_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config": {"kind": "local", "path": "/configs/x86_64-kdump.config"},
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

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if self.fail_on == request.name:
            raise CategorizedError(
                "synthetic put failure",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        key = request.key()
        self.puts.append((key, request.name, request.owner_kind, request.sensitivity))
        return StoredArtifact(
            key, "etag-" + request.name, request.sensitivity, request.retention_class
        )


@dataclass
class _Seams:
    """Injected slow-op seams; default to canned successful outputs."""

    config_text: str = _GOOD_CONFIG
    build_id: bytes = b"\xab\xcd\xef\x01\x23\x45\x67\x89"
    olddefconfig_returncode: int = 0
    make_returncode: int = 0
    olddefconfig_calls: int = 0
    make_calls: int = 0
    checkout_calls: int = 0
    call_order: list[str] = field(default_factory=list)

    def checkout(self, run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        self.checkout_calls += 1
        self.call_order.append("checkout")

    def run_olddefconfig(self, workspace: Path) -> int:
        self.olddefconfig_calls += 1
        self.call_order.append("olddefconfig")
        return self.olddefconfig_returncode

    def read_config(self, workspace: Path) -> str:
        self.call_order.append("read_config")
        return self.config_text

    def run_make(self, workspace: Path) -> int:
        self.make_calls += 1
        self.call_order.append("make")
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
        run_olddefconfig=seams.run_olddefconfig,
        read_config=seams.read_config,
        run_make=seams.run_make,
        read_kernel_image=seams.read_kernel_image,
        read_vmlinux=seams.read_vmlinux,
        read_build_id=seams.read_build_id,
        secret_registry=SecretRegistry(),
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


def test_build_runs_olddefconfig_before_config_validation(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert seams.call_order[:3] == ["checkout", "olddefconfig", "read_config"]
    assert seams.call_order[-1] == "make"


def test_build_maps_olddefconfig_failure_to_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(olddefconfig_returncode=2)

    with pytest.raises(CategorizedError) as exc:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert seams.make_calls == 0
    assert store.puts == []


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


def test_build_rejects_config_missing_profile_requirements_before_store(tmp_path: Path) -> None:
    profile = BuildProfile.parse(
        {
            **_VALID_PROFILE,
            "profile_requirements": {
                "provider": "local-libvirt",
                "name": "console-ready_x86_64",
            },
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    config_text = "\n".join(
        [
            "CONFIG_CRASH_DUMP=y",
            "CONFIG_DEBUG_INFO_DWARF5=y",
            "CONFIG_SERIAL_8250_CONSOLE=y",
            "CONFIG_VIRTIO_BLK=n",
            "CONFIG_VIRTIO_PCI=y",
        ]
    )
    store, seams = _FakeStore(), _Seams(config_text=config_text)

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, profile)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert seams.make_calls == 0
    assert store.puts == []


def test_real_read_config_missing_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _real_read_config(tmp_path)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {"file": ".config"}


# --- make / store failures -----------------------------------------------------------


def test_build_nonzero_make_is_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(make_returncode=2)
    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())
    assert caught.value.category is ErrorCategory.BUILD_FAILURE


def test_build_missing_bzimage_after_make_is_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()
    builder = LocalLibvirtBuild(
        tenant=_TENANT,
        workspace_root=tmp_path,
        store_factory=lambda: store,
        checkout=seams.checkout,
        run_olddefconfig=seams.run_olddefconfig,
        read_config=seams.read_config,
        run_make=seams.run_make,
        read_kernel_image=_real_read_kernel_image,
        read_vmlinux=seams.read_vmlinux,
        read_build_id=seams.read_build_id,
        secret_registry=SecretRegistry(),
    )

    with pytest.raises(CategorizedError) as caught:
        builder.build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert caught.value.details == {"output": "bzImage"}
    assert seams.make_calls == 1
    assert store.puts == []


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
    builder = LocalLibvirtBuild.from_env(
        secret_registry=SecretRegistry()
    )  # building must not spawn make or connect S3
    assert isinstance(builder, LocalLibvirtBuild)


def test_from_env_parses_build_component_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "/srv/kdive/build/components:/mnt/kdive/components",
    )

    builder = LocalLibvirtBuild.from_env(secret_registry=SecretRegistry())

    assert builder._allowed_component_roots == [
        Path("/srv/kdive/build/components"),
        Path("/mnt/kdive/components"),
    ]


def test_from_env_defaults_build_component_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_BUILD_COMPONENT_ROOTS", raising=False)

    builder = LocalLibvirtBuild.from_env(secret_registry=SecretRegistry())

    assert builder._allowed_component_roots == [Path("/var/lib/kdive/build/components")]


def test_validate_config_ref_rejects_local_file_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.config"
    outside.write_text("CONFIG_CRASH_DUMP=y\n", encoding="utf-8")
    builder = LocalLibvirtBuild(
        tenant=_TENANT,
        workspace_root=tmp_path / "workspace",
        store_factory=lambda: _FakeStore(),
        checkout=lambda _run, _profile, _workspace: None,
        run_olddefconfig=lambda _workspace: 0,
        read_config=lambda _workspace: _GOOD_CONFIG,
        run_make=lambda _workspace: 0,
        read_kernel_image=lambda _workspace: b"kernel",
        read_vmlinux=lambda _workspace: b"vmlinux",
        read_build_id=lambda _workspace: "deadbeef",
        secret_registry=SecretRegistry(),
        allowed_component_roots=[allowed],
    )

    with pytest.raises(CategorizedError) as exc:
        builder.validate_config_ref(LocalComponentRef(kind="local", path=str(outside)))

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- real seam argv (the wrappers that only run under live_vm, but whose argv is testable) ----


def test_real_run_make_runs_parallel_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A kernel build must parallelize across cores; an argv without -j serializes it and the
    # build takes ~15x longer on a many-core host.
    captured: list[list[str]] = []

    def _capture(argv: list[str], **__: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _capture)
    assert build_module._real_run_make(Path("/ws")) == 0
    argv = captured[0]
    assert argv[:3] == ["make", "-C", "/ws"]
    assert any(tok.startswith("-j") and tok[2:].isdigit() and int(tok[2:]) >= 1 for tok in argv), (
        argv
    )


def test_real_run_make_timeout_is_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(["make"], timeout=build_module._MAKE_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_run_make(Path("/ws"))

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert caught.value.details["timeout_s"] == build_module._MAKE_TIMEOUT_S


def test_real_run_make_missing_binary_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("make")

    monkeypatch.setattr(subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_run_make(Path("/ws"))

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"tool": "make"}


def test_real_run_make_launch_oserror_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _launch_fault(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError("fork failed")

    monkeypatch.setattr(subprocess, "run", _launch_fault)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_run_make(Path("/ws"))

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"tool": "make", "op": "launch"}


def test_real_read_build_id_reads_merged_notes_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # vmlinux.lds merges every ELF note into one `.notes` section, so the standalone
    # `.note.gnu.build-id` section objcopy looks for in a userspace binary is empty for a
    # vmlinux. Dumping the wrong section yields zero bytes and turns a successful make into a
    # spurious BUILD_FAILURE; objcopy must dump `.notes`.
    build_id = b"\xaa\xbb\xcc\xdd\x01\x02\x03\x04"
    note = _gnu_build_id_note(build_id)
    captured: list[list[str]] = []

    def _fake_objcopy(argv: list[str], **__: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        Path(argv[-1]).write_bytes(note)  # objcopy writes the dumped section to the out path
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_objcopy)
    assert build_module._real_read_build_id(tmp_path) == build_id.hex()
    assert "--only-section=.notes" in captured[0]
    assert "--only-section=.note.gnu.build-id" not in captured[0]


def test_real_read_build_id_objcopy_timeout_is_build_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(["objcopy"], timeout=build_module._OBJCOPY_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_read_build_id(tmp_path)

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert caught.value.details["timeout_s"] == build_module._OBJCOPY_TIMEOUT_S


def test_real_read_build_id_missing_objcopy_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _missing(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("objcopy")

    monkeypatch.setattr(subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_read_build_id(tmp_path)

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {"tool": "objcopy"}


# --- live_vm real-make build ---------------------------------------------------------


@pytest.mark.live_vm
def test_live_vm_real_make_build_id_matches_readelf() -> None:  # pragma: no cover - live_vm
    """Drive the real seams end-to-end and assert the build-id equals ``readelf -n``.

    Runs only on a real build host. Inputs come from the operator/live_vm runner:
    ``KDIVE_KERNEL_SRC`` is the warm tree and ``KDIVE_TEST_BUILD_CONFIG`` is a ``.config``
    (path or ``file://`` URL) that satisfies the kdump/debuginfo preflight. Absent either —
    or ``readelf``/``rsync`` — the test skips, the established gated-suite convention.
    """
    import os
    import re
    import subprocess as sp
    import tempfile

    src = os.environ.get("KDIVE_KERNEL_SRC")
    config_ref = os.environ.get("KDIVE_TEST_BUILD_CONFIG")
    if not src or not config_ref or not shutil.which("readelf") or not shutil.which("rsync"):
        pytest.skip("KDIVE_KERNEL_SRC / KDIVE_TEST_BUILD_CONFIG / readelf / rsync unavailable")

    with tempfile.TemporaryDirectory() as tmp:
        store = _FakeStore()
        builder = LocalLibvirtBuild(
            tenant=_TENANT,
            workspace_root=Path(tmp),
            store_factory=lambda: store,
            checkout=lambda _run, profile, ws: build_module._real_checkout(
                src, profile, ws, secret_registry=SecretRegistry()
            ),
            run_olddefconfig=build_module._real_run_olddefconfig,
            read_config=build_module._real_read_config,
            run_make=build_module._real_run_make,
            read_kernel_image=build_module._real_read_kernel_image,
            read_vmlinux=build_module._real_read_vmlinux,
            read_build_id=build_module._real_read_build_id,
            secret_registry=SecretRegistry(),
        )
        profile = BuildProfile.parse(
            {
                "schema_version": 1,
                "kernel_source_ref": f"file://{src}",
                "config": {"kind": "local", "path": config_ref},
                "patch_ref": None,
            }
        )
        assert isinstance(profile, ServerBuildProfile)
        out = builder.build(_RUN, profile)

        vmlinux = Path(tmp) / str(_RUN) / "vmlinux"
        notes = sp.run(
            ["readelf", "-n", str(vmlinux)], capture_output=True, text=True, check=True
        ).stdout
        match = re.search(r"Build ID:\s*([0-9a-f]+)", notes)
        assert match is not None, "readelf reported no GNU build-id"
        assert out.build_id == match.group(1)


# --- _resolve_local_ref -------------------------------------------------------------


def test_resolve_local_ref_file_url(tmp_path: Path) -> None:
    target = tmp_path / "x.config"
    target.write_text("CONFIG_X=y\n")
    assert _resolve_local_ref(f"file://{target}", kind="config") == target


def test_resolve_local_ref_bare_absolute_path(tmp_path: Path) -> None:
    target = tmp_path / "x.config"
    target.write_text("CONFIG_X=y\n")
    assert _resolve_local_ref(str(target), kind="config") == target


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.com/x.config",
        "git+https://example.com/x#v1",
        "s3://bucket/x.config",
    ],
)
def test_resolve_local_ref_rejects_non_local_scheme(ref: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(ref, kind="config")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_file_url_with_netloc() -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref("file://host/path/x.config", kind="config")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_relative_path() -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref("configs/x.config", kind="config")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(str(tmp_path / "absent.config"), kind="config")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_local_ref_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _resolve_local_ref(str(tmp_path), kind="config")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- _stage_config ------------------------------------------------------------------


def test_stage_config_copies_bytes_to_workspace_dotconfig(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    config = root / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    _stage_config(
        LocalComponentRef(kind="local", path=str(config)),
        workspace,
        allowed_component_roots=[root],
    )

    assert (workspace / ".config").read_text() == "CONFIG_FROM_REF=y\n"


def test_stage_config_overwrites_existing_dotconfig(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".config").write_text("CONFIG_WARM_TREE=y\n")
    root = tmp_path / "components"
    root.mkdir()
    config = root / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    _stage_config(
        LocalComponentRef(kind="local", path=str(config)),
        workspace,
        allowed_component_roots=[root],
    )

    assert (workspace / ".config").read_text() == "CONFIG_FROM_REF=y\n"


def test_stage_config_rejects_sha256_mismatch_before_copy(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    config = root / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    with pytest.raises(CategorizedError) as caught:
        _stage_config(
            LocalComponentRef(kind="local", path=str(config), sha256="sha256:" + "0" * 64),
            workspace,
            allowed_component_roots=[root],
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not (workspace / ".config").exists()


def test_stage_config_copies_config_with_matching_sha256(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    config = root / "x.config"
    content = b"CONFIG_FROM_REF=y\n"
    config.write_bytes(content)
    sha256 = f"sha256:{hashlib.sha256(content).hexdigest()}"

    _stage_config(
        LocalComponentRef(kind="local", path=str(config), sha256=sha256),
        workspace,
        allowed_component_roots=[root],
    )

    assert (workspace / ".config").read_bytes() == content


def test_stage_config_rejects_config_outside_allowed_roots_before_copy(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    outside = tmp_path / "outside.config"
    outside.write_text("CONFIG_FROM_REF=y\n")

    with pytest.raises(CategorizedError) as caught:
        _stage_config(
            LocalComponentRef(kind="local", path=str(outside)),
            workspace,
            allowed_component_roots=[root],
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert not (workspace / ".config").exists()


def test_stage_config_missing_ref_is_configuration_error(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    with pytest.raises(CategorizedError) as caught:
        _stage_config(
            LocalComponentRef(kind="local", path=str(root / "absent.config")),
            workspace,
            allowed_component_roots=[root],
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_stage_config_copy_fault_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    root = tmp_path / "components"
    root.mkdir()
    config = root / "x.config"
    config.write_text("CONFIG_FROM_REF=y\n")

    def _copy_fault(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(build_module.shutil, "copyfile", _copy_fault)

    with pytest.raises(CategorizedError) as caught:
        _stage_config(
            LocalComponentRef(kind="local", path=str(config)),
            workspace,
            allowed_component_roots=[root],
        )

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"op": "copy_config", "path": ".config"}


# --- _apply_patch -------------------------------------------------------------------

_GOOD_PATCH = (
    "--- a/init/main.c\n+++ b/init/main.c\n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2-patched\n"
)
_BAD_PATCH = (
    "--- a/init/main.c\n+++ b/init/main.c\n@@ -1,2 +1,2 @@\n nomatch1\n-nomatch2\n+nomatch3\n"
)


def _workspace_with_target(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / "init").mkdir(parents=True)
    (workspace / "init" / "main.c").write_text("line1\nline2\n")
    return workspace


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_apply_patch_applies_clean_diff(tmp_path: Path) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)

    _apply_patch(str(patch), workspace)

    assert (workspace / "init" / "main.c").read_text() == "line1\nline2-patched\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_apply_patch_bad_diff_is_configuration_error_with_redacted_detail(tmp_path: Path) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "bad.patch"
    patch.write_text(_BAD_PATCH)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    stderr = caught.value.details["stderr"]
    assert isinstance(stderr, str)
    # the raw added patch line is never echoed back through the error detail
    assert "nomatch3" not in stderr


def test_apply_patch_missing_git_is_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: None)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_apply_patch_timeout_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_with_target(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/git")

    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["git", "apply"], timeout=build_module._GIT_APPLY_TIMEOUT_S)

    monkeypatch.setattr(build_module.subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details["timeout_s"] == build_module._GIT_APPLY_TIMEOUT_S


def test_apply_patch_no_tree_change_is_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Content backstop (issue #227): git apply exits 0 without naming a skip on stderr but
    # leaves the tree unchanged, so _apply_patch must fail rather than report a build of an
    # unpatched kernel as success.
    workspace = _workspace_with_target(tmp_path)
    original = (workspace / "init" / "main.c").read_text()
    patch = tmp_path / "fix.patch"
    patch.write_text(_GOOD_PATCH)
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/git")

    def _noop(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_module.subprocess, "run", _noop)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert (workspace / "init" / "main.c").read_text() == original


def test_apply_patch_stderr_skipped_patch_fails_even_when_a_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git apply can emit "Skipped patch 'X'." for an already-applied file while writing
    # another and exiting 0 (issue #227); the stderr guard must reject that even though
    # the tree did change, where the content backstop alone would let it through.
    workspace = _workspace_with_target(tmp_path)
    (workspace / "kernel").mkdir()
    (workspace / "kernel" / "sched.c").write_text("a\n")
    patch = tmp_path / "fix.patch"
    patch.write_text(
        _GOOD_PATCH + "--- a/kernel/sched.c\n+++ b/kernel/sched.c\n@@ -1 +1 @@\n-a\n+b\n"
    )
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/git")

    def _partial_skip(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        (workspace / "kernel" / "sched.c").write_text("b\n")  # one file applies...
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="Skipped patch 'init/main.c'.\nApplied patch kernel/sched.c cleanly.\n",
        )

    monkeypatch.setattr(build_module.subprocess, "run", _partial_skip)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- exit criterion 1: a no-op kernel patch FAILS patch-applied verification (#227) -


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_exit_criterion_noop_patch_fails_patch_applied_verification(tmp_path: Path) -> None:
    # M2.4 exit criterion 1 (closes the #227 class) for the local-libvirt kernel build plane:
    # a no-op kernel patch must FAIL patch-applied verification, driven end to end through the
    # REAL `_apply_patch` (real `git apply` over a `.git`-less workspace — the #227 condition).
    # A patch whose change is already present in the tree is a no-op: shipping it would build an
    # unpatched kernel and report success. Real `git apply` refuses it (returncode != 0), so the
    # apply-result guard rejects it; the test fails if that guard is removed. (The complementary
    # #227 silent-skip face — `git apply` exits 0 yet leaves the tree unchanged — and the
    # content-snapshot backstop that catches it are proven by
    # `test_apply_patch_no_tree_change_is_configuration_error` above.)
    workspace = _workspace_with_target(tmp_path)
    workspace_main = workspace / "init" / "main.c"
    workspace_main.write_text("line1\nline2-patched\n")  # the patch is already applied: a no-op
    original = workspace_main.read_text()
    patch = tmp_path / "noop.patch"
    patch.write_text(_GOOD_PATCH)

    with pytest.raises(CategorizedError) as caught:
        _apply_patch(str(patch), workspace)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert workspace_main.read_text() == original  # the no-op left the tree unchanged


# --- _sync_tree ---------------------------------------------------------------------


def _ok_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_sync_tree_missing_kernel_src_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _sync_tree("", tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_nonexistent_kernel_src_is_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(tmp_path / "absent"), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_relative_kernel_src_is_configuration_error(tmp_path: Path) -> None:
    # A non-absolute kernel_src is rejected before any rsync (no option-injection surface).
    with pytest.raises(CategorizedError) as caught:
        _sync_tree("linux", tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_filesystem_root_is_configuration_error(tmp_path: Path) -> None:
    # kernel_src="/" must never be accepted — it would rsync the entire root filesystem.
    with pytest.raises(CategorizedError) as caught:
        _sync_tree("/", tmp_path / "ws")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_sync_tree_missing_rsync_is_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: None)
    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_sync_tree_timeout_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")

    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["rsync"], timeout=build_module._RSYNC_TIMEOUT_S)

    monkeypatch.setattr(build_module.subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), tmp_path / "ws")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details["timeout_s"] == build_module._RSYNC_TIMEOUT_S


def test_sync_tree_creates_workspace_and_invokes_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    workspace = tmp_path / "runs" / "abc" / "ws"  # parents do not exist yet
    calls: list[list[str]] = []

    def _record(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _ok_run()

    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(build_module.subprocess, "run", _record)

    _sync_tree(str(src), workspace)

    assert workspace.is_dir()  # mkdir(parents=True) ran before rsync
    # `--` terminates option parsing so a path is never read as an rsync flag.
    assert calls == [["rsync", "-a", "--delete", "--", f"{src}/", f"{workspace}/"]]


def test_sync_tree_workspace_mkdir_fault_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()
    workspace = tmp_path / "runs" / "abc" / "ws"
    original_mkdir = Path.mkdir

    def _mkdir_fault(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if self == workspace:
            raise OSError("permission denied")
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(Path, "mkdir", _mkdir_fault)

    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), workspace)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {"op": "mkdir", "path": "build_workspace"}


def test_sync_tree_rsync_nonzero_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "linux"
    src.mkdir()

    def _fail(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=23, stdout="", stderr="rsync: disk full"
        )

    monkeypatch.setattr(build_module.shutil, "which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr(build_module.subprocess, "run", _fail)

    with pytest.raises(CategorizedError) as caught:
        _sync_tree(str(src), tmp_path / "ws")
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "stderr" in caught.value.details


# --- _real_checkout composition (host-free, never skipped) --------------------------


def test_real_checkout_calls_steps_in_order_with_right_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    order: list[str] = []
    seen: dict[str, object] = {}

    def _sync(kernel_src: str, ws: Path, secret_registry: SecretRegistry) -> None:
        del secret_registry
        order.append("sync")
        seen["sync"] = (kernel_src, ws)

    def _stage(config_ref: object, ws: Path, *, allowed_component_roots: list[Path]) -> None:
        order.append("stage")
        seen["stage"] = (config_ref, ws, allowed_component_roots)

    def _patch(patch_ref: str, ws: Path, secret_registry: SecretRegistry) -> None:
        del secret_registry
        order.append("patch")
        seen["patch"] = (patch_ref, ws)

    monkeypatch.setattr(build_module, "_sync_tree", _sync)
    monkeypatch.setattr(build_module, "_stage_config", _stage)
    monkeypatch.setattr(build_module, "_apply_patch", _patch)

    profile = BuildProfile.parse(
        {
            **_VALID_PROFILE,
            "config": {"kind": "local", "path": "/configs/c"},
            "patch_ref": "/patches/p",
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    component_roots = [Path("/build/components")]
    build_module._real_checkout(
        "/src/linux",
        profile,
        workspace,
        secret_registry=SecretRegistry(),
        allowed_component_roots=component_roots,
    )

    assert order == ["sync", "stage", "patch"]
    assert seen["sync"] == ("/src/linux", workspace)
    assert seen["stage"] == (profile.config, workspace, component_roots)
    assert seen["patch"] == ("/patches/p", workspace)


def test_real_checkout_skips_patch_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(build_module, "_sync_tree", lambda *_: order.append("sync"))
    monkeypatch.setattr(
        build_module,
        "_stage_config",
        lambda *_, **__: order.append("stage"),
    )
    monkeypatch.setattr(build_module, "_apply_patch", lambda *_: order.append("patch"))

    profile = _profile()  # patch_ref is None
    build_module._real_checkout(
        "/src/linux", profile, tmp_path / "ws", secret_registry=SecretRegistry()
    )

    assert order == ["sync", "stage"]  # no patch step
