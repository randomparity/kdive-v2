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
_STORAGE_POOL_ENV = "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL"
_DEFAULT_STORAGE_POOL = "default"
_NETWORK_ENV = "KDIVE_REMOTE_LIBVIRT_NETWORK"
_DEFAULT_NETWORK = "default"
_GDB_ADDR_ENV = "KDIVE_REMOTE_LIBVIRT_GDB_ADDR"
_GDB_PORT_MIN_ENV = "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN"
_GDB_PORT_MAX_ENV = "KDIVE_REMOTE_LIBVIRT_GDB_PORT_MAX"
_DEFAULT_GDB_PORT_MIN = 47000
_DEFAULT_GDB_PORT_MAX = 47099


@dataclass(frozen=True, slots=True)
class TlsCertRefs:
    """Secret references (not material) for the mutual-TLS client identity + CA."""

    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str


@dataclass(frozen=True, slots=True)
class RemoteLibvirtConfig:
    """The operator-supplied remote host: validated URI, cert refs, host-level knobs.

    ``storage_pool`` and the gdbstub address/port-range are host topology, not
    per-System profile data (ADR-0080 §5). ``gdb_addr`` has **no default** — the
    listen address is the ACL'd security boundary (ADR-0079) and must be named
    explicitly; provisioning fails closed when it is unset.
    """

    uri: str
    cert_refs: TlsCertRefs
    concurrent_allocation_cap: int
    storage_pool: str = _DEFAULT_STORAGE_POOL
    network: str = _DEFAULT_NETWORK
    gdb_addr: str | None = None
    gdb_port_min: int = _DEFAULT_GDB_PORT_MIN
    gdb_port_max: int = _DEFAULT_GDB_PORT_MAX


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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise CategorizedError(
            f"{name}={raw!r} is not an integer",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


def _gdb_port_env(name: str, default: int) -> int:
    port = _int_env(name, default)
    if port < 1 or port > 65535:
        raise CategorizedError(
            f"{name}={port} is outside 1..65535",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return port


def remote_config_from_env() -> RemoteLibvirtConfig:
    """Read and validate the ``KDIVE_REMOTE_LIBVIRT_*`` operator config.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a missing/blank variable, a
            non-integer allocation cap, a URI that is not mutual-TLS-safe (wrong
            scheme, ``no_verify``, or an operator-set ``pkipath``), or a gdbstub
            port range that is non-integer, outside 1..65535, or inverted.
    """
    uri = _required_env(_URI_ENV)
    validate_remote_uri(uri)
    refs = TlsCertRefs(
        client_cert_ref=_required_env(_CLIENT_CERT_REF_ENV),
        client_key_ref=_required_env(_CLIENT_KEY_REF_ENV),
        ca_cert_ref=_required_env(_CA_CERT_REF_ENV),
    )
    cap = _int_env(_CAP_ENV, _DEFAULT_CAP)
    gdb_port_min = _gdb_port_env(_GDB_PORT_MIN_ENV, _DEFAULT_GDB_PORT_MIN)
    gdb_port_max = _gdb_port_env(_GDB_PORT_MAX_ENV, _DEFAULT_GDB_PORT_MAX)
    if gdb_port_min > gdb_port_max:
        raise CategorizedError(
            f"{_GDB_PORT_MIN_ENV}={gdb_port_min} exceeds {_GDB_PORT_MAX_ENV}={gdb_port_max}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=refs,
        concurrent_allocation_cap=cap,
        storage_pool=os.environ.get(_STORAGE_POOL_ENV) or _DEFAULT_STORAGE_POOL,
        network=os.environ.get(_NETWORK_ENV) or _DEFAULT_NETWORK,
        gdb_addr=os.environ.get(_GDB_ADDR_ENV) or None,
        gdb_port_min=gdb_port_min,
        gdb_port_max=gdb_port_max,
    )
