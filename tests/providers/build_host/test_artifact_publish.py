"""Provider-neutral artifact publishing (ADR-0099/0101).

The publish helper is shared by both build providers: an artifact is either bytes the worker
holds (PUT directly) or a file resident on a build host (presigned PUT, hashed host-side so the
worker never reads its bytes).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.providers.build_host.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    publish_artifact_source,
)
from kdive.providers.build_host.transport import CommandResult

_RUN = UUID("33333333-3333-3333-3333-333333333333")


@dataclass(frozen=True)
class _SizedStub:
    """A bytes-stand-in reporting an over-ceiling ``len`` without allocating that memory."""

    size: int

    def __len__(self) -> int:
        return self.size


@dataclass
class _FakeStore:
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


@dataclass
class _FakeTransport:
    files: dict[str, bytes] = field(default_factory=dict)
    uploaded: list[str] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        if argv[0] == "sha256sum":
            digest = hashlib.sha256(self.files[argv[1]]).hexdigest()
            return CommandResult(returncode=0, stdout=f"{digest}  {argv[1]}\n", stderr="")
        if argv[0] == "stat":
            return CommandResult(returncode=0, stdout=f"{len(self.files[argv[-1]])}\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        self.uploaded.append(path)
        return f"etag-{Path(path).name}"

    # unused protocol members — real bodies keep the strict whole-tree ty gate happy.
    def read_text(self, path: str) -> str:  # pragma: no cover - unused
        return ""

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover - unused
        return b""

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover - unused
        return None

    def clone(self, remote: str, ref: str, dest: str) -> None:  # pragma: no cover - unused
        return None

    def cleanup(self, path: str) -> None:  # pragma: no cover - unused
        return None


def test_bytes_source_puts_with_tenant_owner_sensitivity() -> None:
    store = _FakeStore()
    stored = publish_artifact_source(
        store,
        _RUN,
        "kernel",
        ArtifactBytes(b"img"),
        tenant="local",
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="build",
    )
    assert store.presigns == []
    [req] = store.puts
    assert (req.tenant, req.owner_kind, req.owner_id, req.name) == (
        "local",
        "runs",
        str(_RUN),
        "kernel",
    )
    assert req.data == b"img"
    assert req.sensitivity is Sensitivity.SENSITIVE
    assert req.retention_class == "build"
    assert stored.key == f"local/runs/{_RUN}/kernel"


def test_bytes_source_over_5gib_is_configuration_error_before_put() -> None:
    store = _FakeStore()
    oversize = cast("bytes", _SizedStub(5 * 1024**3 + 1))
    with pytest.raises(CategorizedError) as caught:
        publish_artifact_source(
            store,
            _RUN,
            "kernel",
            ArtifactBytes(oversize),
            tenant="local",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "5 GiB" in str(caught.value)
    assert store.puts == []


def test_remote_file_presigns_base64_sha256_and_uploads() -> None:
    store, transport = _FakeStore(), _FakeTransport()
    content = b"\x1f\x8bbundle"
    path = "/build/kdive-bundle.tar.gz"
    transport.files[path] = content

    stored = publish_artifact_source(
        store,
        _RUN,
        "kernel",
        ArtifactRemoteFile(path=path, transport=transport),
        tenant="remote-libvirt",
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="build",
    )

    assert store.puts == []
    [presign] = store.presigns
    assert presign.key == f"remote-libvirt/runs/{_RUN}/kernel"
    assert presign.sha256 == base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
    assert presign.size_bytes == len(content)
    assert transport.uploaded == [path]
    assert stored.key == presign.key


def test_remote_file_over_5gib_is_configuration_error_before_presign() -> None:
    @dataclass
    class _OversizeStat(_FakeTransport):
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
            if argv[0] == "stat":
                return CommandResult(returncode=0, stdout=f"{5 * 1024**3 + 1}\n", stderr="")
            return super().run(argv, cwd=cwd, timeout_s=timeout_s)

    store, transport = _FakeStore(), _OversizeStat()
    transport.files["/build/huge-bundle.tar.gz"] = b"\x1f\x8bbundle"
    with pytest.raises(CategorizedError) as caught:
        publish_artifact_source(
            store,
            _RUN,
            "kernel",
            ArtifactRemoteFile(path="/build/huge-bundle.tar.gz", transport=transport),
            tenant="remote-libvirt",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "5 GiB" in str(caught.value)
    assert store.presigns == []
    assert transport.uploaded == []


def test_remote_file_sha256sum_nonzero_is_build_failure() -> None:
    @dataclass
    class _FailHash(_FakeTransport):
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
            if argv[0] == "sha256sum":
                return CommandResult(returncode=1, stdout="", stderr="No such file")
            return super().run(argv, cwd=cwd, timeout_s=timeout_s)

    store, transport = _FakeStore(), _FailHash()
    transport.files["/p"] = b"x"
    with pytest.raises(CategorizedError) as caught:
        publish_artifact_source(
            store,
            _RUN,
            "kernel",
            ArtifactRemoteFile(path="/p", transport=transport),
            tenant="local",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        )
    assert caught.value.category is ErrorCategory.BUILD_FAILURE
