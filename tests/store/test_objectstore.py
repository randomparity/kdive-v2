"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.store.objectstore import (
    ObjectStore,
    _normalize_etag,
    object_store_from_env,
    register_artifact_row,
)


def _write_request(
    tenant: str,
    owner_kind: str,
    owner_id: str,
    name: str,
    *,
    data: bytes = b"x",
    sensitivity: Sensitivity = Sensitivity.REDACTED,
    retention_class: str = "vmcore",
) -> ArtifactWriteRequest:
    return ArtifactWriteRequest(
        tenant=tenant,
        owner_kind=owner_kind,
        owner_id=owner_id,
        name=name,
        data=data,
        sensitivity=sensitivity,
        retention_class=retention_class,
    )


def test_normalize_etag_strips_surrounding_quotes() -> None:
    assert _normalize_etag('"abc123"') == "abc123"
    assert _normalize_etag("abc123") == "abc123"


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "name"),
    [
        ("", "vmcore", "oid", "core"),
        ("t", "vmcore", "oid", ""),
        ("t", "with/slash", "oid", "core"),
        ("t", "vmcore", "oid", "bad\nname"),
    ],
)
def test_put_artifact_rejects_invalid_key_component(
    tenant: str, kind: str, object_id: str, name: str
) -> None:
    store = ObjectStore(object(), "bucket")  # client never touched: validation precedes it
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            _write_request(tenant, kind, object_id, name),
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


class _UnreachableClient:
    """A stub S3 client whose calls raise a transport-level ``BotoCoreError``."""

    def put_object(self, **_kwargs: object) -> object:
        raise EndpointConnectionError(endpoint_url="http://unreachable")

    def get_object(self, **_kwargs: object) -> object:
        raise EndpointConnectionError(endpoint_url="http://unreachable")


def test_put_artifact_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_UnreachableClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            _write_request("t", "vmcore", "oid", "core"),
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_get_artifact_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_UnreachableClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _MidStreamFailureClient:
    """A stub whose ``get_object`` succeeds but whose body read fails mid-stream."""

    class _Body:
        def read(self) -> bytes:
            raise ReadTimeoutError(endpoint_url="http://unreachable")

    def get_object(self, **_kwargs: object) -> dict[str, object]:
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
            "Body": _MidStreamFailureClient._Body(),
        }


def test_get_artifact_maps_body_read_failure_to_infrastructure_failure() -> None:
    store = ObjectStore(_MidStreamFailureClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_artifact("t/vmcore/oid/core", "etag")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _RecordingClient:
    """A stub S3 client that records the kwargs of its last ``get_object`` call."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, object] | None = None

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.last_kwargs = kwargs
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "vmcore"},
            "Body": _StaticBody(b"bytes"),
        }


class _StaticBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def test_get_artifact_with_etag_sends_if_match() -> None:
    client = _RecordingClient()
    store = ObjectStore(client, "bucket")

    store.get_artifact("t/vmcore/oid/core", "abc123")

    assert client.last_kwargs is not None
    assert client.last_kwargs.get("IfMatch") == '"abc123"'


def test_get_artifact_none_etag_omits_if_match() -> None:
    client = _RecordingClient()
    store = ObjectStore(client, "bucket")

    fetched = store.get_artifact("t/vmcore/oid/core", None)

    assert client.last_kwargs is not None
    assert "IfMatch" not in client.last_kwargs
    assert fetched.data == b"bytes"


def test_register_artifact_row_maps_stored_and_owner() -> None:
    stored = StoredArtifact("t/vmcore/oid/core", "etag123", Sensitivity.REDACTED, "vmcore")
    owner_id = uuid4()

    row = register_artifact_row(stored, owner_kind="system", owner_id=owner_id)

    assert row.object_key == "t/vmcore/oid/core"
    assert row.etag == "etag123"
    assert row.sensitivity is Sensitivity.REDACTED
    assert row.retention_class == "vmcore"
    assert row.owner_kind == "system"
    assert row.owner_id == owner_id
    # id is minted; created_at/updated_at are populated (advisory pre-insert).
    assert row.id is not None


def test_object_store_from_env_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_object_store_from_env_requires_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.delenv("KDIVE_S3_BUCKET", raising=False)

    with pytest.raises(CategorizedError) as excinfo:
        object_store_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_object_store_from_env_defaults_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "bucket")
    monkeypatch.delenv("KDIVE_S3_REGION", raising=False)

    store = object_store_from_env()

    assert store._client.meta.region_name == "us-east-1"


def test_put_get_round_trip(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        _write_request(key_ns, "vmcore", "sys-1", "core.bin", data=b"payload-bytes"),
    )

    assert '"' not in stored.etag  # stored etag is the bare value
    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.data == b"payload-bytes"


def test_get_artifact_unconditional_reads_without_etag(
    minio_store: ObjectStore, key_ns: str
) -> None:
    stored = minio_store.put_artifact(
        _write_request(
            key_ns,
            "runs",
            "run-1",
            "kernel",
            data=b"bzimage-bytes",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
        ),
    )

    fetched = minio_store.get_artifact(stored.key, None)
    assert fetched.data == b"bzimage-bytes"
    assert fetched.sensitivity is Sensitivity.SENSITIVE


def test_get_artifact_unconditional_missing_key_raises_stale_handle(
    minio_store: ObjectStore, key_ns: str
) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(f"{key_ns}/runs/none/kernel", None)
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_put_uses_the_key_scheme(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        _write_request(key_ns, "vmcore", "oid", "core"),
    )
    assert stored.key == f"{key_ns}/vmcore/oid/core"


def test_sensitivity_persisted_as_object_metadata(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        _write_request(
            key_ns,
            "transcript",
            "sys-1",
            "gdb.log",
            data=b"raw-transcript",
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="transcript",
        ),
    )

    fetched = minio_store.get_artifact(stored.key, stored.etag)
    assert fetched.sensitivity is Sensitivity.SENSITIVE
    assert fetched.retention_class == "transcript"

    raw = minio_store._client.head_object(Bucket=minio_store._bucket, Key=stored.key)
    assert raw["Metadata"]["sensitivity"] == "sensitive"
    assert raw["Metadata"]["retention-class"] == "transcript"


def test_get_with_stale_etag_raises_stale_handle(minio_store: ObjectStore, key_ns: str) -> None:
    stored = minio_store.put_artifact(
        _write_request(key_ns, "vmcore", "sys-1", "core.bin", data=b"payload"),
    )

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(stored.key, "0" * 32)
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_missing_object_raises_stale_handle(minio_store: ObjectStore, key_ns: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(f"{key_ns}/vmcore/none/missing", "abc123")
    assert excinfo.value.category is ErrorCategory.STALE_HANDLE


def test_get_object_without_metadata_raises_infrastructure_failure(
    minio_store: ObjectStore, key_ns: str
) -> None:
    key = f"{key_ns}/vmcore/sys-1/bare"
    resp = minio_store._client.put_object(Bucket=minio_store._bucket, Key=key, Body=b"no-metadata")
    etag = resp["ETag"].strip('"')

    with pytest.raises(CategorizedError) as excinfo:
        minio_store.get_artifact(key, etag)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FakePresignClient:
    """Records ``generate_presigned_url`` calls; pure unit seam (no MinIO needed)."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.minted_url = "https://store.example/presigned"
        self.calls: list[tuple[str, dict[str, str], int, str]] = []
        self._raises = raises

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, str], ExpiresIn: int, HttpMethod: str
    ) -> str:
        if self._raises is not None:
            raise self._raises
        self.calls.append((op, Params, ExpiresIn, HttpMethod))
        return self.minted_url


def test_presign_get_mints_time_boxed_url_for_one_key() -> None:
    client = _FakePresignClient()
    store = ObjectStore(client, "bucket")
    url = store.presign_get("t/vmcore/abc/core", expires_in=600)
    assert url == client.minted_url
    assert client.calls == [
        ("get_object", {"Bucket": "bucket", "Key": "t/vmcore/abc/core"}, 600, "GET")
    ]


@pytest.mark.parametrize("expires_in", [0, -1])
def test_presign_get_rejects_non_positive_expiry(expires_in: int) -> None:
    store = ObjectStore(_FakePresignClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_get("k", expires_in=expires_in)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_presign_get_maps_client_error_to_infrastructure_failure() -> None:
    err = ClientError({"Error": {"Code": "boom"}}, "presign")
    store = ObjectStore(_FakePresignClient(raises=err), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_get("k", expires_in=60)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
