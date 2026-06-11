"""Shared artifact value types used across storage, provider, and MCP boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity


def validate_key_component(label: str, value: str) -> str:
    """Validate one object-key path component: non-empty, no ``/`` or control characters.

    Reused by callers that fold a value into a filesystem path or object key before it reaches
    :func:`artifact_key`, so a traversal-bearing component (``/`` or ``..`` with a slash) is
    rejected with a named ``CONFIGURATION_ERROR`` at the boundary rather than escaping a path.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``value`` is empty or has an illegal char.
    """
    if not value:
        raise CategorizedError(
            f"artifact key component {label!r} must not be empty",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "/" in value or any(ord(char) < 0x20 for char in value):
        raise CategorizedError(
            f"artifact key component {label!r} has an illegal character: {value!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str:
    """Public, validated ``{tenant}/{kind}/{object_id}/{name}`` key."""
    parts = [
        validate_key_component("tenant", tenant),
        validate_key_component("kind", kind),
        validate_key_component("object_id", object_id),
        validate_key_component("name", name),
    ]
    return "/".join(parts)


def owner_prefix(tenant: str, kind: str, object_id: str) -> str:
    """The validated ``{tenant}/{kind}/{object_id}/`` key prefix for an owner's objects."""
    parts = [
        validate_key_component("tenant", tenant),
        validate_key_component("kind", kind),
        validate_key_component("object_id", object_id),
    ]
    return "/".join(parts) + "/"


class StoredArtifact(NamedTuple):
    """A put result: the row's ``key``/``etag`` plus the class written to the object."""

    key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactWriteRequest:
    """Artifact write identity, metadata, and bytes."""

    tenant: str
    owner_kind: str
    owner_id: str
    name: str
    data: bytes
    sensitivity: Sensitivity
    retention_class: str

    def key(self) -> str:
        return artifact_key(self.tenant, self.owner_kind, self.owner_id, self.name)


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactStreamRequest:
    """A streaming artifact write: identity + metadata + an on-disk source ``path``.

    The store reads ``path`` as the PUT body so a large artifact (e.g. a spooled host_dump
    core) is uploaded at constant memory rather than read whole into RAM (ADR-0094). The
    caller owns ``path``'s lifecycle (it is typically a worker temp file deleted after the
    put). ``sha256_b64`` is the base64-encoded SHA-256 of ``path``'s bytes (the caller already
    streams the file once to compute it); the store signs it into the PUT so S3 **rejects** an
    upload whose body does not hash to it — the same end-to-end integrity binding the presigned
    kdump PUT gets, and the value ``head().checksum_sha256`` reads back for the post-put check.
    """

    tenant: str
    owner_kind: str
    owner_id: str
    name: str
    path: Path
    sha256_b64: str
    sensitivity: Sensitivity
    retention_class: str

    def key(self) -> str:
        return artifact_key(self.tenant, self.owner_kind, self.owner_id, self.name)


class FetchedArtifact(NamedTuple):
    """A fetched object's bytes and the class read back from its metadata."""

    data: bytes
    sensitivity: Sensitivity
    retention_class: str


class HeadResult(NamedTuple):
    """An object's stored size, base64 SHA-256 checksum if present, and bare etag."""

    size_bytes: int
    checksum_sha256: str | None
    etag: str


class ObjectListing(NamedTuple):
    """One listed object key and its store last-modified time."""

    key: str
    last_modified: datetime


class PresignedUpload(NamedTuple):
    """A presigned PUT URL plus the headers the client must send for it to validate."""

    url: str
    required_headers: dict[str, str]


@dataclass(frozen=True, kw_only=True, slots=True)
class PresignPutRequest:
    """Object-store presign identity, metadata, checksum, and expiry."""

    key: str
    sha256: str
    size_bytes: int
    sensitivity: Sensitivity
    retention_class: str
    expires_in: int
