"""Default production service-factory assembly (ADR-0091 §2).

The default factory assembles the server-vantage `secret_ref` check from the configured
``secret=True`` settings, resolved against the file-ref backend under ``KDIVE_SECRETS_ROOT``.
A ref that does not resolve is a contract ``fail``; the backend root being absent entirely
is the check's ``error`` boundary, not a ``fail``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import kdive.config as config
from kdive.diagnostics.checks import CheckStatus, SecretRefCheck
from kdive.diagnostics.service import default_service_factory
from kdive.domain.errors import CategorizedError, ErrorCategory


def _set_env(monkeypatch, root: Path, **refs: str) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    for name, value in refs.items():
        monkeypatch.setenv(name, value)
    config.load()


def test_factory_builds_a_service_with_a_secret_ref_check(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    service = default_service_factory(None)
    ids = {c.id for c in service._checks}  # noqa: SLF001 - assert the assembled check set
    assert "secret_ref" in ids


def test_secret_ref_passes_when_every_configured_ref_resolves(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "client.pem").write_text("cert", encoding="utf-8")
    (tmp_path / "client.key").write_text("key", encoding="utf-8")
    (tmp_path / "ca.pem").write_text("ca", encoding="utf-8")
    _set_env(
        monkeypatch,
        tmp_path,
        KDIVE_REMOTE_LIBVIRT_URI="qemu+tls://host/system",
        KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF=str(tmp_path / "client.pem"),
        KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF=str(tmp_path / "client.key"),
        KDIVE_REMOTE_LIBVIRT_CA_CERT_REF=str(tmp_path / "ca.pem"),
    )
    check = next(c for c in default_service_factory(None)._checks if isinstance(c, SecretRefCheck))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS


def test_secret_ref_fails_when_a_configured_ref_is_missing(monkeypatch, tmp_path: Path) -> None:
    _set_env(
        monkeypatch,
        tmp_path,
        KDIVE_REMOTE_LIBVIRT_URI="qemu+tls://host/system",
        KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF=str(tmp_path / "absent.pem"),
        KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF=str(tmp_path / "absent.key"),
        KDIVE_REMOTE_LIBVIRT_CA_CERT_REF=str(tmp_path / "absent-ca.pem"),
    )
    check = next(c for c in default_service_factory(None)._checks if isinstance(c, SecretRefCheck))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix is not None


def test_with_egress_fails_fast_when_no_probe_image_is_wired(monkeypatch, tmp_path: Path) -> None:
    # The default factory has no probe-guest seam (remote needs an operator-staged image until
    # M2.4, ADR-0091), so opting into egress fails fast rather than silently dropping the check.
    _set_env(monkeypatch, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        default_service_factory(None, with_egress=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
