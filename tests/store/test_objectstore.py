"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import (
    ObjectStore,
    StoredArtifact,
    _normalize_etag,
    object_store_from_env,
    register_artifact_row,
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
            tenant,
            kind,
            object_id,
            name,
            data=b"x",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


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
