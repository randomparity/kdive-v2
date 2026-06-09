"""Tests for the remote-libvirt Build plane (ADR-0081).

The remote build mirrors local-libvirt's worker-`make` orchestration but is an independent
module (ADR-0076: no shared layer with the doomed provider) and publishes a single
gzip-compressed vmlinuz+modules install bundle as ``kernel_ref`` plus the ``vmlinux``
debuginfo as ``debuginfo_ref``. Fakes are local to this file — nothing imports
``local_libvirt``.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
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
from kdive.providers.remote_libvirt import build as build_module
from kdive.providers.remote_libvirt.build import RemoteLibvirtBuild
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("33333333-3333-3333-3333-333333333333")
_TENANT = "remote-libvirt"

_VALID_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config": {"kind": "local", "path": "/configs/x86_64-kdump.config"},
    "patch_ref": None,
}

# A .config that satisfies both preflight checks (kdump + debuginfo).
_GOOD_CONFIG = "\n".join(
    ["CONFIG_CRASH_DUMP=y", "CONFIG_DEBUG_INFO=y", "CONFIG_DEBUG_INFO_DWARF5=y"]
)


def _profile() -> ServerBuildProfile:
    profile = BuildProfile.parse(_VALID_PROFILE)
    assert isinstance(profile, ServerBuildProfile)
    return profile


@dataclass
class _FakeStore:
    """Records puts; returns a StoredArtifact echoing the key (no real S3)."""

    puts: list[tuple[str, str, str, Sensitivity, bytes]] = field(default_factory=list)
    fail_on: str | None = None

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if self.fail_on == request.name:
            raise CategorizedError(
                "synthetic put failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        key = request.key()
        self.puts.append((key, request.name, request.owner_kind, request.sensitivity, request.data))
        return StoredArtifact(
            key, "etag-" + request.name, request.sensitivity, request.retention_class
        )


@dataclass
class _Seams:
    """Injected slow-op seams; default to canned successful outputs."""

    config_text: str = _GOOD_CONFIG
    olddefconfig_returncode: int = 0
    make_returncode: int = 0
    modules_install_returncode: int = 0
    build_id_hex: str = "abcdef0123456789"
    bundle_bytes: bytes = b"gzip-bundle-bytes"
    call_order: list[str] = field(default_factory=list)
    make_calls: int = 0
    modules_install_calls: int = 0
    staging_roots: list[Path] = field(default_factory=list)

    def checkout(self, run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        self.call_order.append("checkout")

    def run_olddefconfig(self, workspace: Path) -> int:
        self.call_order.append("olddefconfig")
        return self.olddefconfig_returncode

    def read_config(self, workspace: Path) -> str:
        self.call_order.append("read_config")
        return self.config_text

    def run_make(self, workspace: Path) -> int:
        self.make_calls += 1
        self.call_order.append("make")
        return self.make_returncode

    def run_modules_install(self, workspace: Path, mod_root: Path) -> int:
        self.modules_install_calls += 1
        self.staging_roots.append(mod_root)
        self.call_order.append("modules_install")
        return self.modules_install_returncode

    def build_bundle(self, workspace: Path, mod_root: Path) -> bytes:
        self.call_order.append("bundle")
        return self.bundle_bytes

    def read_vmlinux(self, workspace: Path) -> bytes:
        return b"vmlinux-bytes"

    def read_build_id(self, workspace: Path) -> str:
        return self.build_id_hex


def _builder(store: _FakeStore, seams: _Seams, tmp_path: Path) -> RemoteLibvirtBuild:
    return RemoteLibvirtBuild(
        workspace_root=tmp_path / "ws",
        store_factory=lambda: store,
        checkout=seams.checkout,
        run_olddefconfig=seams.run_olddefconfig,
        read_config=seams.read_config,
        run_make=seams.run_make,
        run_modules_install=seams.run_modules_install,
        build_bundle=seams.build_bundle,
        read_vmlinux=seams.read_vmlinux,
        read_build_id=seams.read_build_id,
        staging_factory=lambda: _make_staging(tmp_path),
    )


def _make_staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging" / "mods"
    root.mkdir(parents=True, exist_ok=True)
    return root


# --- build() happy path --------------------------------------------------------------


def test_build_returns_bundle_and_vmlinux_refs_and_build_id(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    out = _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert out.kernel_ref == f"{_TENANT}/runs/{_RUN}/kernel"
    assert out.debuginfo_ref == f"{_TENANT}/runs/{_RUN}/vmlinux"
    assert out.build_id == seams.build_id_hex
    assert seams.make_calls == 1
    assert seams.modules_install_calls == 1


def test_build_stores_bundle_under_kernel_and_vmlinux_sensitive(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    _builder(store, seams, tmp_path).build(_RUN, _profile())

    by_name = {name: (kind, sens, data) for _, name, kind, sens, data in store.puts}
    assert set(by_name) == {"kernel", "vmlinux"}
    assert by_name["kernel"][2] == seams.bundle_bytes  # the gzip bundle bytes
    assert all(kind == "runs" for kind, _, _ in by_name.values())
    assert all(sens is Sensitivity.SENSITIVE for _, sens, _ in by_name.values())


def test_build_call_order_make_then_modules_then_bundle(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert seams.call_order[:3] == ["checkout", "olddefconfig", "read_config"]
    assert seams.call_order[-3:] == ["make", "modules_install", "bundle"]


# --- failure paths -------------------------------------------------------------------


def test_build_olddefconfig_failure_is_build_failure_nothing_stored(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(olddefconfig_returncode=2)

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert seams.make_calls == 0
    assert store.puts == []


@pytest.mark.parametrize(
    "config_text",
    [
        "CONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF5=y",  # no kdump
        "CONFIG_CRASH_DUMP=y",  # no debuginfo
        "# CONFIG_CRASH_DUMP is not set\nCONFIG_DEBUG_INFO_DWARF5=y",  # explicitly unset
    ],
)
def test_build_rejects_config_missing_prereq_before_make(tmp_path: Path, config_text: str) -> None:
    store, seams = _FakeStore(), _Seams(config_text=config_text)

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert seams.make_calls == 0
    assert store.puts == []


def test_build_nonzero_make_is_build_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(make_returncode=2)

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert seams.modules_install_calls == 0
    assert store.puts == []


def test_build_nonzero_modules_install_is_build_failure_nothing_stored(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(modules_install_returncode=1)

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.BUILD_FAILURE
    assert seams.make_calls == 1
    assert store.puts == []  # bundle never built/stored


def test_build_store_failure_propagates_infrastructure(tmp_path: Path) -> None:
    store, seams = _FakeStore(fail_on="kernel"), _Seams()

    with pytest.raises(CategorizedError) as caught:
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


# --- staging cleanup -----------------------------------------------------------------


def test_build_removes_staging_dir_on_success(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams()

    _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert seams.staging_roots, "modules_install should have received a staging root"
    assert not seams.staging_roots[0].exists()  # removed in finally


def test_build_removes_staging_dir_on_failure(tmp_path: Path) -> None:
    store, seams = _FakeStore(), _Seams(modules_install_returncode=1)

    with pytest.raises(CategorizedError):
        _builder(store, seams, tmp_path).build(_RUN, _profile())

    assert seams.staging_roots
    assert not seams.staging_roots[0].exists()


# --- from_env ------------------------------------------------------------------------


def test_from_env_does_not_spawn_make_or_connect_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive")
    monkeypatch.setenv("KDIVE_BUILD_WORKSPACE", "/tmp/kdive-build")
    monkeypatch.setenv("KDIVE_KERNEL_SRC", "/tmp/kernel-src")

    def _no_spawn(*_: object, **__: object) -> object:
        raise AssertionError("from_env must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    builder = RemoteLibvirtBuild.from_env(secret_registry=SecretRegistry())
    assert isinstance(builder, RemoteLibvirtBuild)


def test_validate_config_ref_rejects_file_outside_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.config"
    outside.write_text("CONFIG_CRASH_DUMP=y\n")
    builder = RemoteLibvirtBuild(
        workspace_root=tmp_path / "ws",
        store_factory=lambda: _FakeStore(),
        checkout=lambda _r, _p, _w: None,
        run_olddefconfig=lambda _w: 0,
        read_config=lambda _w: _GOOD_CONFIG,
        run_make=lambda _w: 0,
        run_modules_install=lambda _w, _m: 0,
        build_bundle=lambda _w, _m: b"b",
        read_vmlinux=lambda _w: b"v",
        read_build_id=lambda _w: "deadbeef",
        staging_factory=lambda: _make_staging(tmp_path),
        allowed_component_roots=[allowed],
    )

    with pytest.raises(CategorizedError) as caught:
        builder.validate_config_ref(LocalComponentRef(kind="local", path=str(outside)))

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


# --- real seams (host-free where possible) -------------------------------------------


def test_real_run_modules_install_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def _capture(argv: list[str], **__: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _capture)
    assert build_module._real_run_modules_install(Path("/ws"), Path("/stage")) == 0
    argv = captured[0]
    assert argv[:3] == ["make", "-C", "/ws"]
    assert "modules_install" in argv
    assert "INSTALL_MOD_PATH=/stage" in argv


def test_real_run_modules_install_timeout_is_build_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(["make"], timeout=build_module._MAKE_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_run_modules_install(Path("/ws"), Path("/stage"))

    assert caught.value.category is ErrorCategory.BUILD_FAILURE


def test_real_run_modules_install_missing_make_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("make")

    monkeypatch.setattr(subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        build_module._real_run_modules_install(Path("/ws"), Path("/stage"))

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY


def _write_fake_build_tree(workspace: Path, mod_root: Path, version: str = "6.9.0") -> None:
    """A minimal workspace + INSTALL_MOD_PATH staging tree for bundle packaging."""
    bzimage = workspace / "arch" / "x86" / "boot" / "bzImage"
    bzimage.parent.mkdir(parents=True)
    bzimage.write_bytes(b"vmlinuz-bytes")
    moddir = mod_root / "lib" / "modules" / version
    (moddir / "kernel" / "drivers").mkdir(parents=True)
    (moddir / "kernel" / "drivers" / "virtio_blk.ko").write_bytes(b"module-bytes")
    (moddir / "modules.dep").write_text("virtio_blk.ko:\n")
    # The back-reference symlinks make modules_install plants (absolute worker paths).
    (moddir / "build").symlink_to(workspace)
    (moddir / "source").symlink_to(workspace)


def test_real_build_bundle_includes_vmlinuz_and_modules_excludes_backrefs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    data = build_module._real_build_bundle(workspace, mod_root)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "boot/vmlinuz" in names
    assert "lib/modules/6.9.0/kernel/drivers/virtio_blk.ko" in names
    # the dangling absolute back-reference symlinks are stripped
    assert "lib/modules/6.9.0/build" not in names
    assert "lib/modules/6.9.0/source" not in names


def test_real_build_bundle_is_valid_gzip(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    mod_root = tmp_path / "stage"
    _write_fake_build_tree(workspace, mod_root)

    data = build_module._real_build_bundle(workspace, mod_root)

    assert data[:2] == b"\x1f\x8b"  # gzip magic


# --- live_vm real make ---------------------------------------------------------------


@pytest.mark.live_vm
def test_live_vm_real_make_bundle_has_modules() -> None:  # pragma: no cover - live_vm
    """Drive the real seams and assert the bundle is a gzip tar with vmlinuz + modules.

    Runs only on a real build host. ``KDIVE_KERNEL_SRC`` is the warm tree and
    ``KDIVE_TEST_BUILD_CONFIG`` a ``.config`` satisfying the kdump/debuginfo preflight;
    absent either (or ``make``), the test skips. The full remote spine is operator-run (#207).
    """
    import os
    import shutil
    import tempfile

    src = os.environ.get("KDIVE_KERNEL_SRC")
    config_ref = os.environ.get("KDIVE_TEST_BUILD_CONFIG")
    if not src or not config_ref or not shutil.which("make"):
        pytest.skip("KDIVE_KERNEL_SRC / KDIVE_TEST_BUILD_CONFIG / make unavailable")

    with tempfile.TemporaryDirectory() as tmp:
        store = _FakeStore()
        builder = RemoteLibvirtBuild(
            workspace_root=Path(tmp),
            store_factory=lambda: store,
            checkout=lambda _run, profile, ws: build_module._real_checkout(
                src, profile, ws, secret_registry=SecretRegistry()
            ),
            run_olddefconfig=build_module._real_run_olddefconfig,
            read_config=build_module._real_read_config,
            run_make=build_module._real_run_make,
            run_modules_install=build_module._real_run_modules_install,
            build_bundle=build_module._real_build_bundle,
            read_vmlinux=build_module._real_read_vmlinux,
            read_build_id=build_module._real_read_build_id,
            staging_factory=lambda: Path(tempfile.mkdtemp(prefix="kdive-mod-")),
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
        builder.build(_RUN, profile)

        bundle = next(data for _, name, _, _, data in store.puts if name == "kernel")
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            names = tar.getnames()
        assert "boot/vmlinuz" in names
        assert any(n.startswith("lib/modules/") and n.endswith(".ko") for n in names)
