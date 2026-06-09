"""Shared artifact value types used across storage, provider, and MCP boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity


def _validate_component(label: str, value: str) -> str:
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
        _validate_component("tenant", tenant),
        _validate_component("kind", kind),
        _validate_component("object_id", object_id),
        _validate_component("name", name),
    ]
    return "/".join(parts)


def owner_prefix(tenant: str, kind: str, object_id: str) -> str:
    """The validated ``{tenant}/{kind}/{object_id}/`` key prefix for an owner's objects."""
    parts = [
        _validate_component("tenant", tenant),
        _validate_component("kind", kind),
        _validate_component("object_id", object_id),
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
