"""Tests for the transport-backed post-make pipeline of RemoteLibvirtBuild (Task 7.5, ADR-0342).

The default (worker-local) path publishes from in-memory bytes via ``put_artifact`` and is
covered in ``test_build.py``. Here we drive the SSH path: the modules_install / objcopy / tar
steps run over a ``BuildTransport``, the build-id note is read back and parsed on the worker,
and the large artifacts publish via presigned PUT — the worker only ever sees a host-computed
sha256, never the bundle or vmlinux bytes.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from kdive.domain.models import Sensitivity
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.provider_components.build_validation import parse_gnu_build_id
from kdive.providers.build_host.transport_seams import (
    transport_read_build_id,
    transport_run_modules_install,
)
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt import build as build_module
from kdive.providers.remote_libvirt.build import (
    ArtifactBytes,
    ArtifactRemoteFile,
    RemoteLibvirtBuild,
    transport_make_bundle,
    transport_vmlinux_source,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("55555555-5555-5555-5555-555555555555")
_TENANT = "remote-libvirt"
_GOOD_CONFIG = "CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF5=y\n"
_FRAGMENT_BYTES = _GOOD_CONFIG.encode()

_VALID_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
    "patch_ref": None,
}


def _profile() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_VALID_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def _build_a_note(build_id_hex: str) -> bytes:
    """Assemble a minimal little-endian ELF note carrying a GNU build-id (round-trips parse)."""
    desc = bytes.fromhex(build_id_hex)
    name = b"GNU\x00"
    header = (
        (4).to_bytes(4, "little")  # namesz: "GNU\0"
        + len(desc).to_bytes(4, "little")  # descsz
        + (3).to_bytes(4, "little")  # NT_GNU_BUILD_ID
    )
    return header + name + desc


@dataclass
class _Call:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass
class _FakeTransport:
    """Records calls; canned run() results keyed by argv head; serves a note + file digests.

    ``files`` maps a host path to its bytes; ``sha256sum`` / ``stat`` over those paths return
    the real digest/size so the hex->base64 conversion is exercised end to end. ``read_bytes``
    serves the objcopy note. A test asserts no ``read_bytes`` is ever issued for the large
    bundle/vmlinux paths (only the small note).
    """

    build_id_hex: str = "abcdef0123456789abcdef0123456789abcdef01"
    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[_Call] = field(default_factory=list)

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(_Call(method, args, kwargs))

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        self._record("run", argv, cwd=cwd, timeout_s=timeout_s)
        head = argv[0]
        if head == "sha256sum":
            digest = hashlib.sha256(self.files[argv[1]]).hexdigest()
            return CommandResult(returncode=0, stdout=f"{digest}  {argv[1]}\n", stderr="")
        if head == "stat":
            return CommandResult(returncode=0, stdout=f"{len(self.files[argv[-1]])}\n", stderr="")
        if head == "objcopy":
            self.files[argv[-1]] = _build_a_note(self.build_id_hex)
            return CommandResult(returncode=0, stdout="", stderr="")
        if head == "tar":
            self.files[argv[2]] = b"\x1f\x8b" + b"gzip-bundle-payload"  # -czf <out>
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def read_text(self, path: str) -> str:  # pragma: no cover - not used here
        return _GOOD_CONFIG

    def read_bytes(self, path: str) -> bytes:
        self._record("read_bytes", path)
        return self.files[path]

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover
        self.files[path] = data

    def clone(self, remote: str, ref: str, dest: str) -> None:  # pragma: no cover
        self._record("clone", remote, ref, dest)

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        self._record("upload_file", path, presigned=presigned)
        return f"etag-{Path(path).name}"

    def cleanup(self, path: str) -> None:  # pragma: no cover
        self._record("cleanup", path)

    def run_argvs(self) -> list[list[str]]:
        return [c.args[0] for c in self.calls if c.method == "run"]

    def read_bytes_paths(self) -> list[str]:
        return [c.args[0] for c in self.calls if c.method == "read_bytes"]


@dataclass
class _FakeStore:
    """Records put_artifact and presign_put; presign echoes the request into the upload."""

    puts: list[ArtifactWriteRequest] = field(default_factory=list)
    presigns: list[PresignPutRequest] = field(default_factory=list)

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.puts.append(request)
        return StoredArtifact(
            request.key(), "etag-" + request.name, request.sensitivity, request.retention_class
        )

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        self.presigns.append(request)
        return PresignedUpload(
            url=f"https://s3.example/{request.key}",
            required_headers={"x-amz-checksum-sha256": request.sha256},
        )


def _transport_builder(
    store: _FakeStore, transport: _FakeTransport, tmp_path: Path
) -> RemoteLibvirtBuild:
    """A builder whose post-make seams are all transport-backed (Task 11's ssh wiring)."""
    return RemoteLibvirtBuild(
        workspace_root=tmp_path / "ws",
        store_factory=lambda: store,
        checkout=lambda _r, _p, _w, _f: None,
        run_olddefconfig=lambda _w: 0,
        read_config=lambda _w: _GOOD_CONFIG,
        run_make=lambda _w: 0,
        run_modules_install=transport_run_modules_install(transport),
        make_bundle=transport_make_bundle(transport),
        read_vmlinux_source=transport_vmlinux_source(transport),
        read_build_id=transport_read_build_id(transport),
        staging_factory=lambda: _make_staging(tmp_path),
        catalog_fetch=lambda _name: _FRAGMENT_BYTES,
    )


def _make_staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging" / "mods"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_artifacts(transport: _FakeTransport, tmp_path: Path) -> tuple[str, str]:
    """Return the bundle + vmlinux paths and pre-place the vmlinux bytes.

    The workspace is ``workspace_root/<run_id>`` (the orchestrator's per-run dir). The bundle
    file is created by the faked ``tar`` step during the build; ``vmlinux`` is seeded here
    because no faked step produces it (the build seam only references it).
    """
    workspace = tmp_path / "ws" / str(_RUN)
    bundle_path = str(workspace / "kdive-bundle.tar.gz")
    vmlinux_path = str(workspace / "vmlinux")
    transport.files[vmlinux_path] = b"\x7fELF" + b"vmlinux-debuginfo-payload"
    return bundle_path, vmlinux_path


# --- happy path: remote publish via presigned PUT ------------------------------------


def test_transport_build_publishes_via_presign_and_returns_keys(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    bundle_path, vmlinux_path = _seed_artifacts(transport, tmp_path)

    out = _transport_builder(store, transport, tmp_path).build(_RUN, _profile())

    assert out.kernel_ref == f"{_TENANT}/runs/{_RUN}/kernel"
    assert out.debuginfo_ref == f"{_TENANT}/runs/{_RUN}/vmlinux"
    assert out.build_id == transport.build_id_hex
    # The remote path publishes via presign + upload, never put_artifact.
    assert store.puts == []
    presigned_keys = {p.key for p in store.presigns}
    assert presigned_keys == {out.kernel_ref, out.debuginfo_ref}
    uploaded = {c.args[0] for c in transport.calls if c.method == "upload_file"}
    assert uploaded == {bundle_path, vmlinux_path}


def test_transport_build_runs_modules_install_objcopy_and_tar_over_transport(
    tmp_path: Path,
) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    _seed_artifacts(transport, tmp_path)

    _transport_builder(store, transport, tmp_path).build(_RUN, _profile())

    heads = [argv[0] for argv in transport.run_argvs()]
    assert "make" in heads  # modules_install
    assert "objcopy" in heads
    assert "tar" in heads
    modules_install = next(a for a in transport.run_argvs() if a[0] == "make")
    assert "modules_install" in modules_install
    assert any(tok.startswith("INSTALL_MOD_PATH=") for tok in modules_install)


def test_transport_build_tar_excludes_backref_symlinks_and_renames_vmlinuz(
    tmp_path: Path,
) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    _seed_artifacts(transport, tmp_path)

    _transport_builder(store, transport, tmp_path).build(_RUN, _profile())

    tar_argv = next(a for a in transport.run_argvs() if a[0] == "tar")
    assert "--exclude=*/build" in tar_argv
    assert "--exclude=*/source" in tar_argv
    assert any("bzImage" in tok and "boot/vmlinuz" in tok for tok in tar_argv)
    assert "lib/modules" in tar_argv


def test_transport_build_worker_never_reads_bundle_or_vmlinux_bytes(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    bundle_path, vmlinux_path = _seed_artifacts(transport, tmp_path)

    _transport_builder(store, transport, tmp_path).build(_RUN, _profile())

    # Only the small objcopy note is ever read back to the worker.
    read_paths = transport.read_bytes_paths()
    assert bundle_path not in read_paths
    assert vmlinux_path not in read_paths
    assert read_paths == [str(tmp_path / "ws" / str(_RUN) / "vmlinux.note")]


def test_transport_build_id_note_parsed_on_worker(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport(build_id_hex="0011223344556677")
    _seed_artifacts(transport, tmp_path)

    out = _transport_builder(store, transport, tmp_path).build(_RUN, _profile())

    assert out.build_id == "0011223344556677"
    # parse_gnu_build_id round-trips the synthesized note.
    assert parse_gnu_build_id(_build_a_note("0011223344556677")) == "0011223344556677"


# --- workspace cleanup routes through the transport ----------------------------------


def test_over_transport_build_removes_clone_dir_via_transport(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    _seed_artifacts(transport, tmp_path)

    base = _transport_builder(store, transport, tmp_path)
    builder = base.over_transport(
        transport,
        host_workspace_root=str(tmp_path / "ws"),
        git_remote="https://git.example/linux.git",
        git_ref="v6.9",
        secret_registry=SecretRegistry(),
    )
    builder.build(_RUN, _profile())

    cleaned = [c.args[0] for c in transport.calls if c.method == "cleanup"]
    assert str(tmp_path / "ws" / str(_RUN)) in cleaned  # the per-run clone dir on the host


# --- sha256 hex -> base64 correctness ------------------------------------------------


def test_publish_remote_file_presigns_base64_of_sha256(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    content = b"\x1f\x8bknown-bundle-content-for-digest"
    path = str(tmp_path / "ws" / "kdive-bundle.tar.gz")
    transport.files[path] = content

    builder = _transport_builder(store, transport, tmp_path)
    stored = builder.publish(_RUN, "kernel", ArtifactRemoteFile(path=path, transport=transport))

    expected_b64 = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
    presign = next(p for p in store.presigns if p.key == stored.key)
    assert presign.sha256 == expected_b64
    assert presign.size_bytes == len(content)
    assert presign.sensitivity is Sensitivity.SENSITIVE
    assert presign.retention_class == "build"
    assert stored.etag == "etag-kdive-bundle.tar.gz"


# --- the bytes source still uses put_artifact (no presign) ---------------------------


def test_publish_bytes_source_uses_put_artifact_not_presign(tmp_path: Path) -> None:
    store, transport = _FakeStore(), _FakeTransport()
    builder = _transport_builder(store, transport, tmp_path)

    stored = builder.publish(_RUN, "kernel", ArtifactBytes(b"in-memory-bundle"))

    assert store.presigns == []
    assert len(store.puts) == 1
    request = store.puts[0]
    assert request.tenant == _TENANT
    assert request.owner_kind == "runs"
    assert request.owner_id == str(_RUN)
    assert request.name == "kernel"
    assert request.data == b"in-memory-bundle"
    assert request.sensitivity is Sensitivity.SENSITIVE
    assert request.retention_class == "build"
    assert stored.key == f"{_TENANT}/runs/{_RUN}/kernel"


# --- failure mapping -----------------------------------------------------------------


def test_transport_sha256sum_nonzero_is_build_failure(tmp_path: Path) -> None:
    @dataclass
    class _FailingHash(_FakeTransport):
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
            if argv[0] == "sha256sum":
                return CommandResult(returncode=1, stdout="", stderr="No such file")
            return super().run(argv, cwd=cwd, timeout_s=timeout_s)

    store, transport = _FakeStore(), _FailingHash()
    path = str(tmp_path / "ws" / "kdive-bundle.tar.gz")
    transport.files[path] = b"payload"
    builder = _transport_builder(store, transport, tmp_path)

    with pytest.raises(build_module.CategorizedError) as caught:
        builder.publish(_RUN, "kernel", ArtifactRemoteFile(path=path, transport=transport))

    assert caught.value.category is build_module.ErrorCategory.BUILD_FAILURE
