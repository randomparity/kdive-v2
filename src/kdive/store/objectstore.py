"""S3-compatible artifact storage for kdive (ADR-0013, ADR-0017).

Writes bulk artifacts under the key scheme ``{tenant}/{kind}/{object_id}/{name}``
with their sensitivity/retention recorded as object metadata, and reads them back
with an etag-consistency check. The client is synchronous (boto3); async callers
offload via ``asyncio.to_thread``. It is policy-neutral — it never decides whether a
fetched object may reach a response (the handler's redaction gate does, using the
returned sensitivity).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import kdive.config as config
from kdive.config.core_settings import S3_BUCKET, S3_ENDPOINT_URL, S3_REGION
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Artifact, Sensitivity
from kdive.provider_components import artifacts as artifact_types
from kdive.provider_components.artifacts import (
    artifact_key as artifact_key,
)
from kdive.provider_components.artifacts import (
    chunk_key as chunk_key,
)
from kdive.provider_components.artifacts import (
    owner_prefix as owner_prefix,
)

# boto3 ships no inline types and boto3-stubs is not a dependency; alias the S3
# client type to Any at this single site rather than add a stubs package.
S3Client = Any

_DEFAULT_REGION = "us-east-1"

# A missing object (404) and an etag mismatch (412) are the one stale_handle case.
_STALE_STATUSES = frozenset({404, 412})


def _normalize_etag(raw: str) -> str:
    return raw.strip('"')


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


def _local_stream_error(key: str, path: str, err: OSError) -> CategorizedError:
    return CategorizedError(
        f"object-store put_stream for {key!r} could not read {path!r}: {err.strerror}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": "put_stream", "key": key, "path": path},
    )


class ObjectStore:
    """A synchronous S3-compatible artifact store bound to one bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        """Write ``data`` under the key scheme; return its key, etag, and class.

        The object carries the request's ``sensitivity`` and ``retention_class`` as user metadata.
        Async callers must offload this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = request.key()
        try:
            resp = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=request.data,
                Metadata={
                    "sensitivity": request.sensitivity.value,
                    "retention-class": request.retention_class,
                },
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("put_object", key, err) from err
        return artifact_types.StoredArtifact(
            key,
            _normalize_etag(resp["ETag"]),
            request.sensitivity,
            request.retention_class,
        )

    def put_stream(
        self, request: artifact_types.ArtifactStreamRequest
    ) -> artifact_types.StoredArtifact:
        """Write ``request.path``'s bytes under the key scheme, streaming from disk.

        Used by callers holding a large artifact on local disk (the spooled host_dump core,
        ADR-0094): the open file handle is the PUT body, so boto3 streams it in chunks rather
        than the whole object being read into RAM. The object carries the request's
        ``sensitivity``/``retention_class`` as user metadata, matching :meth:`put_artifact`,
        and ``request.sha256_b64`` is sent as ``ChecksumSHA256`` so S3 rejects the PUT if the
        streamed body does not hash to it (the end-to-end integrity binding) and a later
        ``head`` returns it for the caller's post-put verification. Async callers must offload
        this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = request.key()
        try:
            with request.path.open("rb") as body:
                resp = self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ChecksumSHA256=request.sha256_b64,
                    Metadata={
                        "sensitivity": request.sensitivity.value,
                        "retention-class": request.retention_class,
                    },
                )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("put_object", key, err) from err
        except OSError as err:
            raise _local_stream_error(key, str(request.path), err) from err
        return artifact_types.StoredArtifact(
            key,
            _normalize_etag(resp["ETag"]),
            request.sensitivity,
            request.retention_class,
        )

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
        """Fetch the object at ``key``, optionally guarded by an ``If-Match`` on ``etag``.

        When ``etag`` is a bare value (from :class:`StoredArtifact`), the GET is
        conditional — the client-serving path's stale-handle check (ADR-0017 §3): a 412
        mismatch raises ``STALE_HANDLE``. When ``etag`` is ``None`` the GET is
        unconditional, for callers that hold a key the system itself produced and no
        client handle to validate (the install staging fetch and the symbolization
        fetches, ADR-0054); a 404 still raises ``STALE_HANDLE``. Async callers must
        offload via ``asyncio.to_thread``.

        Raises:
            CategorizedError: the object is missing or (with an ``etag``) no longer
                matches (:attr:`ErrorCategory.STALE_HANDLE`); the object lacks
                interpretable sensitivity metadata, or the get otherwise fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        get_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if etag is not None:
            get_kwargs["IfMatch"] = f'"{etag}"'
        try:
            resp = self._client.get_object(**get_kwargs)
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
        return artifact_types.FetchedArtifact(data, sensitivity, retention_class)

    def ping(self) -> None:
        """Probe the bucket's reachability with a ``HEAD`` (ADR-0090 §5 readiness check).

        Raises:
            CategorizedError: the bucket is unreachable or absent
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`). Async callers offload via
                ``asyncio.to_thread``.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("head_bucket", self._bucket, err) from err

    def head(self, key: str) -> artifact_types.HeadResult | None:
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
        return artifact_types.HeadResult(
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
        self, request: artifact_types.PresignPutRequest
    ) -> artifact_types.PresignedUpload:
        """Mint a presigned PUT that signs the checksum + object metadata into the URL.

        The agent must send the returned ``required_headers`` (the signed
        ``x-amz-checksum-sha256`` and ``x-amz-meta-*`` metadata); S3 rejects a PUT whose
        checksum disagrees with the signed value, and the metadata lands on the object so
        the later install fetch (`get_artifact`) reads its sensitivity. This mints a single
        PUT (the 5 GiB single-object ceiling on real S3); ``size_bytes`` is recorded by the
        caller's manifest and capped to that ceiling before this is called. The `live_stack`
        test asserts the **checksum** binding, not the upload length (ADR-0048 §2).

        Raises:
            CategorizedError: presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        metadata = {
            "sensitivity": request.sensitivity.value,
            "retention-class": request.retention_class,
        }
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": request.key,
                    "ChecksumSHA256": request.sha256,
                    "Metadata": metadata,
                },
                ExpiresIn=request.expires_in,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_put", request.key, err) from err
        headers = {
            "x-amz-checksum-sha256": request.sha256,
            "x-amz-meta-sensitivity": request.sensitivity.value,
            "x-amz-meta-retention-class": request.retention_class,
        }
        return artifact_types.PresignedUpload(url=url, required_headers=headers)

    def presign_get(self, key: str, *, expires_in: int) -> str:
        """Mint a time-boxed presigned GET URL for one object (ADR-0076, ADR-0078).

        The URL is a bearer capability scoped to ``key`` alone, expiring after
        ``expires_in`` seconds. Callers that hand it across a trust boundary must
        register it in the redaction registry before it leaves the worker
        (ADR-0078 §2 — the in-target seam).

        Raises:
            CategorizedError: ``expires_in`` is not positive
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`), or presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        if expires_in <= 0:
            raise CategorizedError(
                f"presign_get for {key!r} needs a positive expiry, got {expires_in}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"key": key},
            )
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_get", key, err) from err

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        """Initiate a multipart upload for ``key``, setting object metadata at create time.

        Metadata cannot be attached at completion, so the sensitivity/retention-class are set
        here and ride onto the reassembled object (ADR-0104 §4). No checksum algorithm is set,
        so the final object carries an ETag but no whole-object checksum. Returns the upload id.

        Raises:
            CategorizedError: the call fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.create_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                Metadata={"sensitivity": sensitivity.value, "retention-class": retention_class},
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("create_multipart_upload", key, err) from err
        return resp["UploadId"]

    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str:
        """Copy ``source_key`` into part ``part_number`` of ``key``'s multipart upload.

        A server-side copy — no bytes transit the process. Returns the part ETag.

        Raises:
            CategorizedError: the copy fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.upload_part_copy(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource={"Bucket": self._bucket, "Key": source_key},
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("upload_part_copy", key, err) from err
        return _normalize_etag(resp["CopyPartResult"]["ETag"])

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str:
        """Complete ``key``'s multipart upload with the ordered ``(part_number, etag)`` list.

        Returns the final object ETag (a multipart ``-N`` form).

        Raises:
            CategorizedError: completion fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        multipart = {"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in parts]}
        try:
            resp = self._client.complete_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id, MultipartUpload=multipart
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("complete_multipart_upload", key, err) from err
        return _normalize_etag(resp["ETag"])

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """Abort ``key``'s multipart upload (best-effort cleanup of a failed reassembly).

        Raises:
            CategorizedError: the abort fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("abort_multipart_upload", key, err) from err

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

    def list_image_objects(self) -> list[artifact_types.ObjectListing]:
        """Return every object under the ``images/`` prefix with its store mtime (paginated).

        Backs the reconciler's leaked-image sweep: the mtime lets the sweep compare an
        orphan object's age against the publish grace in Postgres ``now()``.

        Raises:
            CategorizedError: the listing fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        listings: list[artifact_types.ObjectListing] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix="images/"):
                for obj in page.get("Contents", []):
                    listings.append(
                        artifact_types.ObjectListing(
                            key=obj["Key"], last_modified=obj["LastModified"]
                        )
                    )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_objects_v2", "images/", err) from err
        return listings

    def head_present(self, key: str) -> bool:
        """Return whether an object exists at ``key`` (a HEAD presence check).

        Raises:
            CategorizedError: any non-404 store error
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        return self.head(key) is not None


def register_artifact_row(
    stored: artifact_types.StoredArtifact, *, owner_kind: str, owner_id: UUID
) -> Artifact:
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
    endpoint_url = config.get(S3_ENDPOINT_URL)
    if not endpoint_url:
        raise CategorizedError(
            f"{S3_ENDPOINT_URL.name} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    bucket = config.get(S3_BUCKET)
    if not bucket:
        raise CategorizedError(
            f"{S3_BUCKET.name} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    region = config.get(S3_REGION) or _DEFAULT_REGION
    client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
    return ObjectStore(client, bucket)
