"""Tests for the ingestion-lane object-store methods (ADR-0048)."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import HeadResult, ObjectStore


class _HeadClient:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self._response = response

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
    )


def test_head_returns_size_checksum_and_etag() -> None:
    store = ObjectStore(
        _HeadClient(
            {
                "ContentLength": 42,
                "ChecksumSHA256": "Zm9vYmFy",
                "ETag": '"abc123"',
            }
        ),
        "bucket",
    )
    result = store.head("t/runs/r1/kernel")
    assert result == HeadResult(size_bytes=42, checksum_sha256="Zm9vYmFy", etag="abc123")


def test_head_missing_object_returns_none() -> None:
    store = ObjectStore(_HeadClient(_not_found()), "bucket")
    assert store.head("t/runs/r1/kernel") is None


def test_head_without_checksum_metadata_yields_none_checksum() -> None:
    store = ObjectStore(_HeadClient({"ContentLength": 7, "ETag": '"e"'}), "bucket")
    result = store.head("t/runs/r1/kernel")
    assert result is not None and result.checksum_sha256 is None


def test_head_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_HeadClient(EndpointConnectionError(endpoint_url="http://x")), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.head("t/runs/r1/kernel")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
