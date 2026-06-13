"""Unit tests for the SSH build-host reachability prober (ADR-0103).

No real SSH, network, or secret-file resolution. ``materialized_ssh_identity`` and
``SshBuildTransport.check_reachable`` are patched at the reachability module's namespace so
the prober's scope-management and fail-closed behavior are exercised deterministically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from kdive.db.build_hosts import BuildHost
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.build_host.reachability import BuildHostProber, SshBuildHostProber
from kdive.providers.build_host.ssh_transport import SshBuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry

_MODULE = "kdive.providers.build_host.reachability"
_FAKE_KEY = "-----BEGIN KEY-----\nfake\n-----END KEY-----"  # pragma: allowlist secret


def _ssh_host(
    *, credential_ref: str | None = "cred-ref", address: str | None = "10.0.0.1"
) -> BuildHost:
    return BuildHost(
        id=uuid4(),
        name="builder-1",
        kind="ssh",
        address=address,
        ssh_credential_ref=credential_ref,
        base_image_volume=None,
        workspace_root="/build",
        max_concurrent=2,
        state="ready",
        enabled=True,
    )


def test_probe_returns_true_when_reachable() -> None:
    """probe → True when the underlying check_reachable returns True."""
    registry = SecretRegistry()
    prober = SshBuildHostProber(secret_registry=registry)

    fake_transport = MagicMock()
    fake_transport.check_reachable.return_value = True

    with (
        patch(f"{_MODULE}.materialized_ssh_identity") as mat,
        patch(f"{_MODULE}.SshBuildTransport", return_value=fake_transport),
    ):
        mat.return_value.__enter__.return_value = Path("/tmp/id.pem")  # noqa: S108
        result = asyncio.run(prober.probe(_ssh_host()))

    assert result is True
    fake_transport.check_reachable.assert_called_once()


def test_probe_returns_false_when_unreachable() -> None:
    """probe → False when the underlying check_reachable returns False."""
    prober = SshBuildHostProber(secret_registry=SecretRegistry())

    fake_transport = MagicMock()
    fake_transport.check_reachable.return_value = False

    with (
        patch(f"{_MODULE}.materialized_ssh_identity") as mat,
        patch(f"{_MODULE}.SshBuildTransport", return_value=fake_transport),
    ):
        mat.return_value.__enter__.return_value = Path("/tmp/id.pem")  # noqa: S108
        result = asyncio.run(prober.probe(_ssh_host()))

    assert result is False


def test_probe_none_credential_returns_false_without_transport() -> None:
    """A host missing its credential ref → False, and no transport is constructed."""
    prober = SshBuildHostProber(secret_registry=SecretRegistry())

    with patch(f"{_MODULE}.SshBuildTransport") as transport_cls:
        result = asyncio.run(prober.probe(_ssh_host(credential_ref=None)))

    assert result is False
    transport_cls.assert_not_called()


def test_probe_no_registry_growth_across_passes() -> None:
    """Each probe registers and releases its per-probe scope; the registry is steady-state.

    Exercises the real materialized_ssh_identity register/release cycle by stubbing only
    _resolve_ssh_key (no secret-file resolution) and check_reachable (no subprocess). After
    N probes the registry snapshot must equal the pre-probe baseline — no leaked credential.
    """
    registry = SecretRegistry()
    baseline = registry.snapshot()
    prober = SshBuildHostProber(secret_registry=registry)
    host = _ssh_host()

    with (
        patch(
            "kdive.providers.build_host.ssh_transport._resolve_ssh_key",
            return_value=_FAKE_KEY,
        ),
        patch.object(SshBuildTransport, "check_reachable", return_value=True),
    ):
        for _ in range(5):
            assert asyncio.run(prober.probe(host)) is True

    assert registry.snapshot() == baseline


def test_probe_categorized_error_returns_false_and_releases() -> None:
    """A credential-resolution CategorizedError → False (not raised); scope left clean."""
    registry = SecretRegistry()
    baseline = registry.snapshot()
    prober = SshBuildHostProber(secret_registry=registry)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise CategorizedError(
            "cannot resolve credential", category=ErrorCategory.CONFIGURATION_ERROR
        )

    with patch(f"{_MODULE}.materialized_ssh_identity", side_effect=_raise):
        result = asyncio.run(prober.probe(_ssh_host()))

    assert result is False
    assert registry.snapshot() == baseline


def test_ssh_prober_satisfies_port_protocol() -> None:
    """SshBuildHostProber is a structural BuildHostProber."""
    assert isinstance(SshBuildHostProber(secret_registry=SecretRegistry()), BuildHostProber)
