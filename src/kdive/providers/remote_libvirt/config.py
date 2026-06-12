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

from dataclasses import dataclass

import kdive.config as config
from kdive.config.registry import Setting
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.settings import (
    REMOTE_LIBVIRT_ALLOCATION_CAP,
    REMOTE_LIBVIRT_CA_CERT_REF,
    REMOTE_LIBVIRT_CLIENT_CERT_REF,
    REMOTE_LIBVIRT_CLIENT_KEY_REF,
    REMOTE_LIBVIRT_GDB_ADDR,
    REMOTE_LIBVIRT_GDB_PORT_MAX,
    REMOTE_LIBVIRT_GDB_PORT_MIN,
    REMOTE_LIBVIRT_MACHINE,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_STORAGE_POOL,
    REMOTE_LIBVIRT_URI,
)
from kdive.providers.remote_libvirt.uri_validation import validate_remote_uri

_DEFAULT_CAP = 1
_DEFAULT_STORAGE_POOL = "default"
_DEFAULT_NETWORK = "default"
# i440fx by default: under q35, libvirt places each virtio device behind an
# auto-added pcie-root-port, and on QEMU 10.x those devices can come up in
# D3cold ("Unable to change power state from D3cold to D0, device inaccessible"),
# so the virtio root disk never appears and the guest hangs in the initramfs.
# i440fx puts virtio on the legacy PCI bus and sidesteps it. Operators who need
# q35 can set KDIVE_REMOTE_LIBVIRT_MACHINE=q35 once their host topology powers
# the root ports correctly.
_DEFAULT_MACHINE = "pc"
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
    machine: str = _DEFAULT_MACHINE
    gdb_addr: str | None = None
    gdb_port_min: int = _DEFAULT_GDB_PORT_MIN
    gdb_port_max: int = _DEFAULT_GDB_PORT_MAX


def is_remote_libvirt_configured() -> bool:
    """True when the operator supplied a remote host URI (the composition opt-in gate)."""
    return bool(config.get(REMOTE_LIBVIRT_URI))


def _required_env(setting: Setting[str]) -> str:
    value = config.get(setting)
    if not value:
        raise CategorizedError(
            f"{setting.name} is not set; the remote-libvirt provider needs it",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def _int_env(setting: Setting[str], default: int) -> int:
    raw = config.get(setting)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise CategorizedError(
            f"{setting.name}={raw!r} is not an integer",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


def _gdb_port_env(setting: Setting[str], default: int) -> int:
    port = _int_env(setting, default)
    if port < 1 or port > 65535:
        raise CategorizedError(
            f"{setting.name}={port} is outside 1..65535",
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
    uri = _required_env(REMOTE_LIBVIRT_URI)
    validate_remote_uri(uri)
    refs = TlsCertRefs(
        client_cert_ref=_required_env(REMOTE_LIBVIRT_CLIENT_CERT_REF),
        client_key_ref=_required_env(REMOTE_LIBVIRT_CLIENT_KEY_REF),
        ca_cert_ref=_required_env(REMOTE_LIBVIRT_CA_CERT_REF),
    )
    cap = _int_env(REMOTE_LIBVIRT_ALLOCATION_CAP, _DEFAULT_CAP)
    gdb_port_min = _gdb_port_env(REMOTE_LIBVIRT_GDB_PORT_MIN, _DEFAULT_GDB_PORT_MIN)
    gdb_port_max = _gdb_port_env(REMOTE_LIBVIRT_GDB_PORT_MAX, _DEFAULT_GDB_PORT_MAX)
    if gdb_port_min > gdb_port_max:
        raise CategorizedError(
            f"{REMOTE_LIBVIRT_GDB_PORT_MIN.name}={gdb_port_min} exceeds "
            f"{REMOTE_LIBVIRT_GDB_PORT_MAX.name}={gdb_port_max}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return RemoteLibvirtConfig(
        uri=uri,
        cert_refs=refs,
        concurrent_allocation_cap=cap,
        storage_pool=config.get(REMOTE_LIBVIRT_STORAGE_POOL) or _DEFAULT_STORAGE_POOL,
        network=config.get(REMOTE_LIBVIRT_NETWORK) or _DEFAULT_NETWORK,
        machine=config.get(REMOTE_LIBVIRT_MACHINE) or _DEFAULT_MACHINE,
        gdb_addr=config.get(REMOTE_LIBVIRT_GDB_ADDR) or None,
        gdb_port_min=gdb_port_min,
        gdb_port_max=gdb_port_max,
    )
