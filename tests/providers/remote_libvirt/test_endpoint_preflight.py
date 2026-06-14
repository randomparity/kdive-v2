"""Unit tests for the guest-routable S3 endpoint preflight (#375, ADR-0105)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.endpoint_preflight import (
    validate_guest_routable_endpoint,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9000",
        "https://localhost",
        "localhost:9000",
        "http://LOCALHOST:9000",
        "http://127.0.0.1:9000",
        "http://127.0.0.2:9000",
        "http://[::1]:9000",
        "127.0.0.1:9000",
    ],
)
def test_loopback_endpoint_is_configuration_error(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", endpoint)
    with pytest.raises(CategorizedError) as excinfo:
        validate_guest_routable_endpoint()
    err = excinfo.value
    assert err.category is ErrorCategory.CONFIGURATION_ERROR
    assert err.details["env_var"] == "KDIVE_S3_ENDPOINT_URL"
    assert err.details["configured_endpoint"] == endpoint
    # The remediation names the exact env var as a literal identifier, not just prose.
    assert "KDIVE_S3_ENDPOINT_URL" in str(err.details["next_action"])


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.5:9000",
        "https://minio.svc.cluster.local",
        "http://192.168.2.99:9000",
        "https://s3.example.com",
        "minio.internal:9000",
    ],
)
def test_routable_endpoint_passes(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", endpoint)
    validate_guest_routable_endpoint()  # does not raise


def test_unset_endpoint_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # "unset" is owned by object_store_from_env; the preflight must not double-report it.
    monkeypatch.delenv("KDIVE_S3_ENDPOINT_URL", raising=False)
    validate_guest_routable_endpoint()  # does not raise


def test_blank_endpoint_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "")
    validate_guest_routable_endpoint()  # does not raise
