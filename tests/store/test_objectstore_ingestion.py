"""Tests for the ingestion-lane object-store methods (ADR-0048)."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import (
    HeadResult,
    ObjectStore,
    PresignedUpload,
    artifact_key,
    owner_prefix,
)


class _HeadClient:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self._response = response

    def head_object(self, **_kwargs: object) -> dict[str, object]:
        assert _kwargs.get("ChecksumMode") == "ENABLED"
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
    )


def _forbidden() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "HeadObject",
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


def test_head_non_404_client_error_raises_infrastructure_failure() -> None:
    store = ObjectStore(_HeadClient(_forbidden()), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.head("t/runs/r1/kernel")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_artifact_key_and_owner_prefix_match_layout() -> None:
    assert artifact_key("local", "runs", "r1", "kernel") == "local/runs/r1/kernel"
    assert owner_prefix("local", "runs", "r1") == "local/runs/r1/"


class _RangeClient:
    def get_object(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["Range"] == "bytes=0-3"

        class _Body:
            def read(self) -> bytes:
                return b"\x7fELF"

        return {"Body": _Body()}


def test_get_range_requests_byte_range() -> None:
    store = ObjectStore(_RangeClient(), "bucket")
    assert store.get_range("t/runs/r1/vmlinux", start=0, length=4) == b"\x7fELF"


class _PresignClient:
    def __init__(self) -> None:
        self.params: dict[str, object] | None = None

    def generate_presigned_url(
        self, op: str, *, Params: dict[str, object], ExpiresIn: int, HttpMethod: str
    ) -> str:
        assert op == "put_object" and HttpMethod == "PUT"
        self.params = Params
        return f"https://store/put?exp={ExpiresIn}"


def test_presign_put_signs_checksum_and_metadata() -> None:
    client = _PresignClient()
    store = ObjectStore(client, "bucket")
    out = store.presign_put(
        "local/runs/r1/kernel",
        sha256="Zm9vYmFy",
        size_bytes=10,
        sensitivity=Sensitivity.SENSITIVE,
        retention_class="build",
        expires_in=900,
    )
    assert isinstance(out, PresignedUpload)
    assert out.url == "https://store/put?exp=900"
    assert client.params is not None
    assert client.params["ChecksumSHA256"] == "Zm9vYmFy"
    assert client.params["Metadata"] == {
        "sensitivity": "sensitive",
        "retention-class": "build",
    }
    assert out.required_headers["x-amz-checksum-sha256"] == "Zm9vYmFy"
    assert out.required_headers["x-amz-meta-sensitivity"] == "sensitive"
    assert out.required_headers["x-amz-meta-retention-class"] == "build"


class _FailingGetClient:
    def get_object(self, **_kwargs: object) -> dict[str, object]:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_get_range_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingGetClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.get_range("t/runs/r1/vmlinux", start=0, length=4)
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FailingPresignClient:
    def generate_presigned_url(self, *_a: object, **_k: object) -> str:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_presign_put_maps_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingPresignClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.presign_put(
            "local/runs/r1/kernel",
            sha256="Zm9v",
            size_bytes=10,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class="build",
            expires_in=900,
        )
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_owner_prefix_rejects_invalid_component() -> None:
    with pytest.raises(CategorizedError):
        owner_prefix("local", "runs", "bad/id")


class _ListClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self._pages = pages
        self.deleted: list[str] = []

    def get_paginator(self, op: str) -> object:
        assert op == "list_objects_v2"
        pages = self._pages

        class _Paginator:
            def paginate(self, **_kwargs: object):
                yield from pages

        return _Paginator()

    def delete_object(self, **kwargs: object) -> dict[str, object]:
        self.deleted.append(str(kwargs["Key"]))
        return {}


def test_list_prefix_flattens_pages() -> None:
    client = _ListClient(
        [
            {"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]},
            {"Contents": [{"Key": "p/c"}]},
            {},  # empty page (no Contents) tolerated
        ]
    )
    store = ObjectStore(client, "bucket")
    assert store.list_prefix("p/") == ["p/a", "p/b", "p/c"]


def test_delete_calls_delete_object() -> None:
    client = _ListClient([])
    store = ObjectStore(client, "bucket")
    store.delete("p/a")
    assert client.deleted == ["p/a"]


class _FailingListClient:
    def get_paginator(self, _op: str) -> object:
        class _Paginator:
            def paginate(self, **_kwargs: object):
                raise EndpointConnectionError(endpoint_url="http://x")
                yield  # make this a generator

        return _Paginator()


def test_list_prefix_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingListClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.list_prefix("p/")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


class _FailingDeleteClient:
    def delete_object(self, **_kwargs: object) -> dict[str, object]:
        raise EndpointConnectionError(endpoint_url="http://x")


def test_delete_maps_transport_error_to_infrastructure_failure() -> None:
    store = ObjectStore(_FailingDeleteClient(), "bucket")
    with pytest.raises(CategorizedError) as excinfo:
        store.delete("p/a")
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
