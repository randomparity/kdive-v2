"""Operator configuration for the remote-libvirt provider (ADR-0076, ADR-0077).

The provider is opt-in: composition registers it only when the operator supplies a
``qemu+tls://`` host URI. The TLS client cert, key, and CA are secrets-by-reference
(``SecretBackend`` refs), never material. Reading the config is deferred to
discovery/connection time so the runtime stays buildable without it (ADR-0076).
This env config is authoritative for connections; the discovered resource's
``capabilities`` row is advertisory (insert-if-absent, refreshed only by
re-registration).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.transport import validate_remote_uri

_URI_ENV = "KDIVE_REMOTE_LIBVIRT_URI"
_CLIENT_CERT_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF"
_CLIENT_KEY_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF"  # pragma: allowlist secret - env name
_CA_CERT_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF"
_CAP_ENV = "KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP"
_DEFAULT_CAP = 1


@dataclass(frozen=True, slots=True)
class TlsCertRefs:
    """Secret references (not material) for the mutual-TLS client identity + CA."""

    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str


@dataclass(frozen=True, slots=True)
class RemoteLibvirtConfig:
    """The operator-supplied remote host: validated URI, cert refs, allocation cap."""

    uri: str
    cert_refs: TlsCertRefs
    concurrent_allocation_cap: int


def is_remote_libvirt_configured() -> bool:
    """True when the operator supplied a remote host URI (the composition opt-in gate)."""
    return bool(os.environ.get(_URI_ENV))


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CategorizedError(
            f"{name} is not set; the remote-libvirt provider needs it",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def remote_config_from_env() -> RemoteLibvirtConfig:
    """Read and validate the ``KDIVE_REMOTE_LIBVIRT_*`` operator config.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a missing/blank variable, a
            non-integer allocation cap, or a URI that is not mutual-TLS-safe
            (wrong scheme, ``no_verify``, or an operator-set ``pkipath``).
    """
    uri = _required_env(_URI_ENV)
    validate_remote_uri(uri)
    refs = TlsCertRefs(
        client_cert_ref=_required_env(_CLIENT_CERT_REF_ENV),
        client_key_ref=_required_env(_CLIENT_KEY_REF_ENV),
        ca_cert_ref=_required_env(_CA_CERT_REF_ENV),
    )
    raw_cap = os.environ.get(_CAP_ENV)
    if raw_cap is None:
        cap = _DEFAULT_CAP
    else:
        try:
            cap = int(raw_cap)
        except ValueError:
            raise CategorizedError(
                f"{_CAP_ENV}={raw_cap!r} is not an integer",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None
    return RemoteLibvirtConfig(uri=uri, cert_refs=refs, concurrent_allocation_cap=cap)
