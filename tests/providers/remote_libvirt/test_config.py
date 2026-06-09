"""Operator config for the remote-libvirt provider (ADR-0076, ADR-0077)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    is_remote_libvirt_configured,
    remote_config_from_env,
)

_ENV = {
    "KDIVE_REMOTE_LIBVIRT_URI": "qemu+tls://host.example/system",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF": "remote/clientcert.pem",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF": "remote/clientkey.pem",  # pragma: allowlist secret
    "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF": "remote/cacert.pem",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    merged: dict[str, str | None] = {**_ENV, **overrides}
    for name, value in merged.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_full_env_builds_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    config = remote_config_from_env()
    assert config.uri == "qemu+tls://host.example/system"
    assert config.cert_refs.client_cert_ref == "remote/clientcert.pem"
    assert config.cert_refs.client_key_ref == "remote/clientkey.pem"  # pragma: allowlist secret
    assert config.cert_refs.ca_cert_ref == "remote/cacert.pem"
    assert config.concurrent_allocation_cap == 1  # default


def test_configured_detection_tracks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_URI=None)
    assert not is_remote_libvirt_configured()
    _set_env(monkeypatch)
    assert is_remote_libvirt_configured()


@pytest.mark.parametrize(
    "missing",
    [
        "KDIVE_REMOTE_LIBVIRT_URI",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",
        "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
    ],
)
def test_missing_env_is_configuration_error(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _set_env(monkeypatch, **{missing: None})
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert missing in str(excinfo.value)


def test_non_integer_cap_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP="two")
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_explicit_cap_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP="4")
    assert remote_config_from_env().concurrent_allocation_cap == 4


def test_uri_with_no_verify_is_rejected_at_config_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(
        monkeypatch,
        KDIVE_REMOTE_LIBVIRT_URI="qemu+tls://host.example/system?no_verify=1",
    )
    with pytest.raises(CategorizedError) as excinfo:
        remote_config_from_env()
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
