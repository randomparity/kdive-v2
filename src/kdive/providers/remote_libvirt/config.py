"""Operator configuration for the remote-libvirt provider (ADR-0076, ADR-0077, ADR-0112).

The provider is opt-in: composition registers it only when a ``[[remote_libvirt]]`` instance is
declared in the ``systems.toml`` inventory (ADR-0112). The connection identity — URI, TLS client
cert/key/CA refs (secrets-by-reference, never material), gdbstub listen address, base image, and
per-host allocation cap — is resolved **per op** from that reconciled inventory instance, never
from the removed ``KDIVE_REMOTE_LIBVIRT_*`` singleton env vars (M2.6 Phase 3, #395). Reading the
config is deferred to discovery/connection time so the runtime stays buildable without it
(ADR-0076).

The libvirt host knobs that the v2 inventory model does not carry — storage pool, network, and
QEMU machine type — remain operational ``KDIVE_REMOTE_LIBVIRT_*`` env settings (they are host
topology, not declarative inventory).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import kdive.config as config
from kdive.config.core_settings import SYSTEMS_TOML
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import RemoteLibvirtInstance
from kdive.providers.remote_libvirt.settings import (
    REMOTE_LIBVIRT_MACHINE,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_STORAGE_POOL,
)
from kdive.providers.remote_libvirt.uri_validation import validate_remote_uri

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


@dataclass(frozen=True, slots=True)
class TlsCertRefs:
    """Secret references (not material) for the mutual-TLS client identity + CA."""

    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str


@dataclass(frozen=True, slots=True)
class RemoteLibvirtConfig:
    """The resolved remote host: validated URI, cert refs, host-level knobs.

    ``uri`` / ``cert_refs`` / ``gdb_addr`` / the gdbstub port range / ``concurrent_allocation_cap``
    come from the declared ``[[remote_libvirt]]`` inventory instance (ADR-0112). ``storage_pool`` /
    ``network`` / ``machine`` are host topology not in the v2 model (ADR-0080 §5) and keep their
    operational env defaults. ``gdb_addr`` is the ACL'd security boundary (ADR-0079) and is always
    present when sourced from the inventory (the instance field is required).
    """

    uri: str
    cert_refs: TlsCertRefs
    concurrent_allocation_cap: int
    storage_pool: str = _DEFAULT_STORAGE_POOL
    network: str = _DEFAULT_NETWORK
    machine: str = _DEFAULT_MACHINE
    gdb_addr: str | None = None
    gdb_port_min: int = 47000
    gdb_port_max: int = 47099


def _systems_toml_path() -> Path:
    return Path(config.get(SYSTEMS_TOML) or "./systems.toml")


def _load_remote_instances() -> list[RemoteLibvirtInstance]:
    """Load the ``[[remote_libvirt]]`` instances from ``systems.toml``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the inventory file is present but
            unreadable/malformed/invalid (the parse error is surfaced verbatim).
    """
    try:
        doc = load_inventory_optional(_systems_toml_path())
    except InventoryError as exc:
        raise CategorizedError(
            f"systems.toml is present but invalid: {exc}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    if doc is None:
        return []
    return list(doc.remote_libvirt)


def is_remote_libvirt_configured() -> bool:
    """True when ``systems.toml`` declares at least one ``[[remote_libvirt]]`` instance.

    This is the composition opt-in gate, invoked at app/CLI startup. It **degrades** rather than
    raises: a missing inventory file means "nothing declared" (not configured), and a
    present-but-malformed file is treated as not-configured here too — so a bad operator edit to
    the shared ``systems.toml`` cannot crash the whole MCP server or the unrelated providers
    (ADR-0112's fault-isolation contract). The precise parse error still surfaces fail-closed at
    op time via :func:`remote_config_from_inventory`.
    """
    try:
        return bool(_load_remote_instances())
    except CategorizedError:
        return False


def _resolve_instance() -> RemoteLibvirtInstance:
    """Resolve the single declared remote-libvirt instance for a per-op connection.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no ``[[remote_libvirt]]`` instance is
            declared, or when more than one is — the per-op call path carries no resource
            identity, so it cannot select among multiple remote-libvirt hosts (the
            allocation → resource → instance threading is future work). Failing closed here is
            safer than silently dispatching an op to the wrong host.
    """
    instances = _load_remote_instances()
    if not instances:
        raise CategorizedError(
            "no [[remote_libvirt]] instance is declared in systems.toml; the remote-libvirt "
            "provider needs one",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if len(instances) > 1:
        names = sorted(inst.name for inst in instances)
        raise CategorizedError(
            "multiple [[remote_libvirt]] instances are declared "
            f"({names}); per-op selection among multiple remote-libvirt hosts is not wired",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return instances[0]


def _parse_gdbstub_range(instance: RemoteLibvirtInstance) -> tuple[int, int]:
    """Parse the instance ``gdbstub_range`` (``"min:max"``) into a validated ``(min, max)``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the range is not ``min:max`` of integers,
            a port is outside 1..65535, or the range is inverted.
    """
    raw = instance.gdbstub_range
    parts = raw.split(":")
    if len(parts) != 2:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} is not 'min:max'",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        low, high = int(parts[0]), int(parts[1])
    except ValueError:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} has non-integer ports",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    for port in (low, high):
        if port < 1 or port > 65535:
            raise CategorizedError(
                f"remote_libvirt[{instance.name}].gdbstub_range port {port} is outside 1..65535",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
    if low > high:
        raise CategorizedError(
            f"remote_libvirt[{instance.name}].gdbstub_range={raw!r} is inverted",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return low, high


def remote_config_from_inventory() -> RemoteLibvirtConfig:
    """Resolve the remote-libvirt connection config from the ``systems.toml`` instance.

    Maps the declared ``[[remote_libvirt]]`` instance onto :class:`RemoteLibvirtConfig`: ``uri``
    (validated mutual-TLS-safe), the three TLS cert/key/CA refs, ``gdb_addr``, the
    ``gdbstub_range`` parsed into the port bounds, and ``concurrent_allocation_cap``. The libvirt
    storage pool / network / machine knobs come from the operational env settings (they are not in
    the v2 inventory model).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no instance (or more than one) is declared,
            the inventory file is malformed, the URI is not mutual-TLS-safe (wrong scheme,
            ``no_verify``, or an operator-set ``pkipath``), or the gdbstub range is malformed,
            out of range, or inverted.
    """
    instance = _resolve_instance()
    validate_remote_uri(instance.uri)
    gdb_port_min, gdb_port_max = _parse_gdbstub_range(instance)
    return RemoteLibvirtConfig(
        uri=instance.uri,
        cert_refs=TlsCertRefs(
            client_cert_ref=instance.client_cert_ref,
            client_key_ref=instance.client_key_ref,
            ca_cert_ref=instance.ca_cert_ref,
        ),
        concurrent_allocation_cap=instance.concurrent_allocation_cap,
        storage_pool=config.get(REMOTE_LIBVIRT_STORAGE_POOL) or _DEFAULT_STORAGE_POOL,
        network=config.get(REMOTE_LIBVIRT_NETWORK) or _DEFAULT_NETWORK,
        machine=config.get(REMOTE_LIBVIRT_MACHINE) or _DEFAULT_MACHINE,
        gdb_addr=instance.gdb_addr,
        gdb_port_min=gdb_port_min,
        gdb_port_max=gdb_port_max,
    )
