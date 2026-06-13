"""Provider-neutral build-artifact publishing (ADR-0099/0101).

An artifact is either bytes the worker holds (PUT directly) or a file resident on a build host
(presigned PUT, hashed host-side so the worker never reads its bytes). Both build providers
build over the neutral ``build_host`` layer; this is its publish primitive (ADR-0076 bars only
provider<->provider coupling, not use of this shared layer).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import kdive.config as config
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
    artifact_key,
)
from kdive.providers.build_host.transport import BuildTransport

_MAX_PRESIGN_TTL_S = 3600
_MAX_SINGLE_PUT_BYTES = 5 * 1024**3


@dataclass(slots=True, frozen=True)
class ArtifactBytes:
    """An artifact the worker holds in memory and publishes with a direct PUT."""

    data: bytes


@dataclass(slots=True, frozen=True)
class ArtifactRemoteFile:
    """An artifact that lives on a build host and publishes via a presigned PUT.

    Attributes:
        path: Absolute path to the file on the build host.
        transport: The transport that can hash and upload that file.
    """

    path: str
    transport: BuildTransport


type ArtifactSource = ArtifactBytes | ArtifactRemoteFile


class StorePort(Protocol):
    """The store surface the publish helper needs: a direct PUT and a presigned PUT."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...


def publish_artifact_source(
    store: StorePort,
    run_id: UUID,
    name: str,
    source: ArtifactSource,
    *,
    tenant: str,
    sensitivity: Sensitivity,
    retention_class: str,
) -> StoredArtifact:
    """Publish one build artifact under ``<tenant>/runs/<run_id>/<name>`` and return its row.

    An :class:`ArtifactBytes` source is PUT directly from worker memory (the historical path).
    An :class:`ArtifactRemoteFile` source is published via a presigned PUT whose checksum is
    computed on the build host, so the worker never reads the file's bytes.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if an artifact's size exceeds the 5 GiB
            single-PUT ceiling (checked against ``len(data)`` for bytes, host-side ``stat`` for a
            remote file); ``INFRASTRUCTURE_FAILURE`` propagated from a failed store operation or
            presigned upload; ``BUILD_FAILURE`` if the host-side hash/size of a remote file
            cannot be read.
    """
    match source:
        case ArtifactBytes(data=data):
            if len(data) > _MAX_SINGLE_PUT_BYTES:
                raise CategorizedError(
                    "build artifact exceeds the single-PUT 5 GiB ceiling",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"run_id": str(run_id), "name": name, "size_bytes": len(data)},
                )
            return store.put_artifact(
                ArtifactWriteRequest(
                    tenant=tenant,
                    owner_kind="runs",
                    owner_id=str(run_id),
                    name=name,
                    data=data,
                    sensitivity=sensitivity,
                    retention_class=retention_class,
                )
            )
        case ArtifactRemoteFile(path=path, transport=transport):
            return _publish_remote_file(
                store,
                run_id,
                name,
                path,
                transport,
                tenant=tenant,
                sensitivity=sensitivity,
                retention_class=retention_class,
            )


def _publish_remote_file(
    store: StorePort,
    run_id: UUID,
    name: str,
    path: str,
    transport: BuildTransport,
    *,
    tenant: str,
    sensitivity: Sensitivity,
    retention_class: str,
) -> StoredArtifact:
    """Presign a PUT for a host-resident file and have the transport upload it.

    The sha256 and byte size are read off the build host; ``presign_put`` signs the base64
    sha256 into the URL so S3 rejects a body that does not hash to it. The worker only ever
    handles the host-computed digest, never the file's bytes. A file over the single-PUT
    5 GiB S3 ceiling is rejected with a typed error before presigning, rather than failing
    opaquely mid-upload (mirrors the host_dump capture's ``_enforce_ceiling``).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the host-side size exceeds the 5 GiB
            single-PUT ceiling; ``BUILD_FAILURE`` if the host-side hash/size cannot be read;
            ``INFRASTRUCTURE_FAILURE`` from a failed presign or upload.
    """
    sha256_b64 = _remote_sha256_b64(transport, path)
    size_bytes = _remote_size_bytes(transport, path)
    if size_bytes > _MAX_SINGLE_PUT_BYTES:
        raise CategorizedError(
            "build artifact exceeds the single-PUT 5 GiB ceiling",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id), "name": name, "size_bytes": size_bytes},
        )
    key = artifact_key(tenant, "runs", str(run_id), name)
    presigned = store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=sha256_b64,
            size_bytes=size_bytes,
            sensitivity=sensitivity,
            retention_class=retention_class,
            expires_in=_presign_ttl_seconds(),
        )
    )
    etag = transport.upload_file(path, presigned)
    return StoredArtifact(key, etag, sensitivity, retention_class)


def _remote_sha256_b64(transport: BuildTransport, path: str) -> str:
    """Hash ``path`` on the build host with ``sha256sum`` and return the base64 digest.

    ``sha256sum`` prints ``"<hex>  <path>"``; the hex is parsed off the first field and
    converted to base64 of the raw 32-byte digest — the form ``presign_put`` signs into the
    ``x-amz-checksum-sha256`` header.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if ``sha256sum`` exits non-zero or prints output
            that does not parse to a 32-byte hex digest.
    """
    result = transport.run(["sha256sum", path], cwd=str(Path(path).parent), timeout_s=300)
    if result.returncode != 0:
        raise CategorizedError(
            "sha256sum of a build artifact exited non-zero",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "stderr": result.stderr[-512:]},
        )
    hex_digest = result.stdout.split()[0] if result.stdout.split() else ""
    try:
        raw = bytes.fromhex(hex_digest)
    except ValueError as exc:
        raise CategorizedError(
            "sha256sum produced an unparseable digest",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path},
        ) from exc
    if len(raw) != 32:
        raise CategorizedError(
            "sha256sum produced a digest of the wrong length",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "len": len(raw)},
        )
    return base64.b64encode(raw).decode("ascii")


def _remote_size_bytes(transport: BuildTransport, path: str) -> int:
    """Return the byte size of ``path`` on the build host via ``stat -c %s``.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if ``stat`` exits non-zero or prints a
            non-integer size.
    """
    result = transport.run(["stat", "-c", "%s", path], cwd=str(Path(path).parent), timeout_s=60)
    if result.returncode != 0:
        raise CategorizedError(
            "stat of a build artifact exited non-zero",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "stderr": result.stderr[-512:]},
        )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise CategorizedError(
            "stat produced a non-integer size",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path},
        ) from exc


def _presign_ttl_seconds() -> int:
    """The presigned-PUT lifetime: ``KDIVE_UPLOAD_TTL_SECONDS`` capped at the S3 max of 3600."""
    return min(_MAX_PRESIGN_TTL_S, config.require(UPLOAD_TTL_SECONDS))


__all__ = [
    "ArtifactBytes",
    "ArtifactRemoteFile",
    "ArtifactSource",
    "StorePort",
    "publish_artifact_source",
]
