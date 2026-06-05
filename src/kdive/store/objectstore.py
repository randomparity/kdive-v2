"""S3-compatible artifact storage for kdive (ADR-0013, ADR-0017).

Writes bulk artifacts under the key scheme ``{tenant}/{kind}/{object_id}/{name}``
with their sensitivity/retention recorded as object metadata, and reads them back
with an etag-consistency check. The client is synchronous (boto3); async callers
offload via ``asyncio.to_thread``. It is policy-neutral — it never decides whether a
fetched object may reach a response (the handler's redaction gate does, using the
returned sensitivity).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Artifact, Sensitivity

# boto3 ships no inline types and boto3-stubs is not a dependency; alias the S3
# client type to Any at this single site rather than add a stubs package.
S3Client = Any

_ENDPOINT_URL_ENV = "KDIVE_S3_ENDPOINT_URL"
_BUCKET_ENV = "KDIVE_S3_BUCKET"
_REGION_ENV = "KDIVE_S3_REGION"
_DEFAULT_REGION = "us-east-1"

# A missing object (404) and an etag mismatch (412) are the one stale_handle case.
_STALE_STATUSES = frozenset({404, 412})


class StoredArtifact(NamedTuple):
    """A put result: the row's ``key``/``etag`` plus the class written to the object."""

    key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str


class FetchedArtifact(NamedTuple):
    """A fetched object's bytes and the class read back from its metadata."""

    data: bytes
    sensitivity: Sensitivity
    retention_class: str


class HeadResult(NamedTuple):
    """An object's stored size, base64 SHA-256 checksum (if any), and bare etag."""

    size_bytes: int
    checksum_sha256: str | None
    etag: str


class PresignedUpload(NamedTuple):
    """A presigned PUT URL plus the headers the client must send for it to validate."""

    url: str
    required_headers: dict[str, str]


def _normalize_etag(raw: str) -> str:
    """Strip S3's surrounding double quotes from an ETag, leaving the bare value."""
    return raw.strip('"')


def _validate_component(label: str, value: str) -> str:
    """Return ``value`` if it is a safe single key segment, else raise.

    Raises:
        CategorizedError: ``value`` is empty or contains ``/`` or a control
            character (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
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


def _artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str:
    """Build the validated ``{tenant}/{kind}/{object_id}/{name}`` object key."""
    return "/".join(
        (
            _validate_component("tenant", tenant),
            _validate_component("kind", kind),
            _validate_component("object_id", object_id),
            _validate_component("name", name),
        )
    )


def artifact_key(tenant: str, kind: str, object_id: str, name: str) -> str:
    """Public, validated ``{tenant}/{kind}/{object_id}/{name}`` key (ingestion + reaper)."""
    return _artifact_key(tenant, kind, object_id, name)


def owner_prefix(tenant: str, kind: str, object_id: str) -> str:
    """The validated ``{tenant}/{kind}/{object_id}/`` key prefix for an owner's objects."""
    base = "/".join(
        (
            _validate_component("tenant", tenant),
            _validate_component("kind", kind),
            _validate_component("object_id", object_id),
        )
    )
    return base + "/"


def _infrastructure_error(op: str, key: str, err: BotoCoreError | ClientError) -> CategorizedError:
    """Map an S3 client or transport error to a typed infrastructure failure.

    ``ClientError`` carries an S3 error code in its ``response``; a ``BotoCoreError``
    (connection refused, DNS failure, connect/read timeout) has no response, so its
    exception class name stands in for the code.
    """
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "unknown")
    else:
        code = type(err).__name__
    return CategorizedError(
        f"object-store {op} for {key!r} failed: {code}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"key": key, "s3_error_code": code},
    )


class ObjectStore:
    """A synchronous S3-compatible artifact store bound to one bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

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
        """Write ``data`` under the key scheme; return its key, etag, and class.

        The object carries ``sensitivity`` and ``retention_class`` as user metadata.
        Async callers must offload this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = _artifact_key(tenant, kind, object_id, name)
        try:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                Metadata={
                    "sensitivity": sensitivity.value,
                    "retention-class": retention_class,
                },
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("put_object", key, err) from err
        return StoredArtifact(key, _normalize_etag(resp["ETag"]), sensitivity, retention_class)

    def get_artifact(self, key: str, etag: str) -> FetchedArtifact:
        """Fetch the object at ``key`` iff its etag still matches ``etag``.

        ``etag`` is the bare value from :class:`StoredArtifact`; the conditional GET
        re-quotes it for the ``If-Match`` header. Async callers must offload via
        ``asyncio.to_thread``.

        Raises:
            CategorizedError: the object is missing or the etag no longer matches
                (:attr:`ErrorCategory.STALE_HANDLE`); the object lacks interpretable
                sensitivity metadata, or the get otherwise fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key, IfMatch=f'"{etag}"')
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in _STALE_STATUSES:
                raise CategorizedError(
                    f"artifact {key!r} is gone or its etag no longer matches",
                    category=ErrorCategory.STALE_HANDLE,
                    details={"key": key, "http_status": status},
                ) from err
            raise _infrastructure_error("get_object", key, err) from err
        except BotoCoreError as err:
            raise _infrastructure_error("get_object", key, err) from err
        metadata = resp["Metadata"]
        try:
            sensitivity = Sensitivity(metadata["sensitivity"])
            retention_class = metadata["retention-class"]
        except (KeyError, ValueError) as err:
            raise CategorizedError(
                f"artifact {key!r} has absent or invalid sensitivity metadata",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            ) from err
        try:
            data = resp["Body"].read()
        except (BotoCoreError, ClientError) as err:
            # The download streams here, after the headers; a mid-stream timeout or
            # dropped connection raises a BotoCoreError that must stay typed too.
            raise _infrastructure_error("get_object", key, err) from err
        return FetchedArtifact(data, sensitivity, retention_class)

    def head(self, key: str) -> HeadResult | None:
        """Return the object's size/checksum/etag, or ``None`` if it does not exist.

        Requests ``ChecksumMode="ENABLED"`` so a checksum written at PUT is returned.

        Raises:
            CategorizedError: any non-404 store error
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return None
            raise _infrastructure_error("head_object", key, err) from err
        except BotoCoreError as err:
            raise _infrastructure_error("head_object", key, err) from err
        return HeadResult(
            size_bytes=int(resp["ContentLength"]),
            checksum_sha256=resp.get("ChecksumSHA256"),
            etag=_normalize_etag(resp["ETag"]),
        )

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        """Return ``length`` bytes of ``key`` starting at ``start`` (an HTTP ranged GET).

        Raises:
            CategorizedError: the ranged read fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        end = start + length - 1
        try:
            resp = self._client.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end}"
            )
            return resp["Body"].read()
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_range", key, err) from err

    def presign_put(
        self,
        key: str,
        *,
        sha256: str,
        size_bytes: int,
        sensitivity: Sensitivity,
        retention_class: str,
        expires_in: int,
    ) -> PresignedUpload:
        """Mint a presigned PUT that signs the checksum + object metadata into the URL.

        The agent must send the returned ``required_headers`` (the signed
        ``x-amz-checksum-sha256`` and ``x-amz-meta-*`` metadata); S3 rejects a PUT whose
        checksum disagrees with the signed value, and the metadata lands on the object so
        the later install fetch (`get_artifact`) reads its sensitivity. ``size_bytes`` is
        recorded by the caller's manifest and capped before this is called; presigned-PUT
        length enforcement is asserted by the `live_stack` test (ADR-0048 §2).

        Raises:
            CategorizedError: presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        metadata = {"sensitivity": sensitivity.value, "retention-class": retention_class}
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ChecksumSHA256": sha256,
                    "Metadata": metadata,
                },
                ExpiresIn=expires_in,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_put", key, err) from err
        headers = {
            "x-amz-checksum-sha256": sha256,
            "x-amz-meta-sensitivity": sensitivity.value,
            "x-amz-meta-retention-class": retention_class,
        }
        return PresignedUpload(url=url, required_headers=headers)

    def list_prefix(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix`` (paginated), or ``[]``.

        Raises:
            CategorizedError: the listing fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_objects_v2", prefix, err) from err
        return keys

    def delete(self, key: str) -> None:
        """Delete ``key`` (idempotent — deleting an absent key is not an error).

        Raises:
            CategorizedError: the delete fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("delete_object", key, err) from err


def register_artifact_row(stored: StoredArtifact, *, owner_kind: str, owner_id: UUID) -> Artifact:
    """Build the ``artifacts`` row for a stored object (no database access).

    The sensitivity/retention come from ``stored`` so the row matches the object by
    construction. The caller inserts and commits it after the object write
    (ADR-0005 write-before-commit). Timestamps are advisory — the DB overwrites them
    on insert (ADR-0016).
    """
    now = datetime.now(UTC)
    return Artifact(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        owner_kind=owner_kind,
        owner_id=owner_id,
        object_key=stored.key,
        etag=stored.etag,
        sensitivity=stored.sensitivity,
        retention_class=stored.retention_class,
    )


def object_store_from_env() -> ObjectStore:
    """Build an :class:`ObjectStore` from the ``KDIVE_S3_*`` environment.

    Reads ``KDIVE_S3_ENDPOINT_URL``, ``KDIVE_S3_BUCKET``, and ``KDIVE_S3_REGION``
    (default ``us-east-1`` — boto3 signs with SigV4 and needs a region). Credentials
    come from boto3's default chain (the standard ``AWS_*`` vars).

    Raises:
        CategorizedError: ``KDIVE_S3_ENDPOINT_URL`` or ``KDIVE_S3_BUCKET`` is unset
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    endpoint_url = os.environ.get(_ENDPOINT_URL_ENV)
    if not endpoint_url:
        raise CategorizedError(
            f"{_ENDPOINT_URL_ENV} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    bucket = os.environ.get(_BUCKET_ENV)
    if not bucket:
        raise CategorizedError(
            f"{_BUCKET_ENV} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    region = os.environ.get(_REGION_ENV) or _DEFAULT_REGION
    client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
    return ObjectStore(client, bucket)
