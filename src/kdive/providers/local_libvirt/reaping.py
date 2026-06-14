"""Local-libvirt reconciler ``InfraReaper`` adapter (ADR-0111).

Realizes the reconciler's :class:`~kdive.providers.reaping.InfraReaper` port over the
local-libvirt discovery + provisioning planes, so the periodic ``leaked_domains`` sweep
actually reaches the local host's domains. ``list_owned`` adapts each
:class:`~kdive.providers.ports.OwnedInfra` row (``{system_id: str, domain_name: str}``) into
the reconciler's ``OwnedDomain`` shape (``name`` + ``system_id: UUID | None``); an empty or
unparseable ``system_id`` becomes ``None`` (never ``UUID("")``, which raises) so the
reconciler falls back to the deterministic name to resolve a genuinely orphaned domain.
``destroy`` routes to the provisioning teardown (destroy + undefine + overlay reclaim),
idempotent over an already-absent domain. Both ports are synchronous, so the blocking calls
are offloaded with :func:`asyncio.to_thread`. Construction is lazy (``from_env`` opens no
connection), so the reaper is safe to assemble unconditionally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.ports import OwnedInfra
from kdive.providers.reaping import OwnedDomain


@dataclass(frozen=True, slots=True)
class _OwnedDomain:
    """The reconciler ``OwnedDomain`` shape (``name`` + optional System id)."""

    name: str
    system_id: UUID | None


class _Discovery(Protocol):
    def list_owned(self) -> list[OwnedInfra]: ...


class _Provisioning(Protocol):
    def teardown(self, domain_name: str) -> None: ...


def _uuid_or_none(value: str) -> UUID | None:
    """Parse ``value`` to a ``UUID``; an empty or invalid string is ``None`` (never raises)."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _to_owned_domain(infra: OwnedInfra) -> OwnedDomain:
    return _OwnedDomain(
        name=infra["domain_name"],
        system_id=_uuid_or_none(infra["system_id"]),
    )


class LibvirtInfraReaper:
    """The reconciler ``InfraReaper`` port backed by the local-libvirt provider ports."""

    def __init__(self, *, discovery: _Discovery, provisioning: _Provisioning) -> None:
        self._discovery = discovery
        self._provisioning = provisioning

    @classmethod
    def from_env(cls) -> LibvirtInfraReaper:
        """Build from the local-libvirt env; opens no connection here."""
        return cls(
            discovery=LocalLibvirtDiscovery.from_env(),
            provisioning=LocalLibvirtProvisioning.from_env(),
        )

    async def list_owned(self) -> list[OwnedDomain]:
        """List the host's kdive-owned domains in the reconciler ``OwnedDomain`` shape."""
        infra = await asyncio.to_thread(self._discovery.list_owned)
        return [_to_owned_domain(item) for item in infra]

    async def destroy(self, name: str) -> None:
        """Destroy + undefine the domain (and reclaim its overlay); idempotent if absent."""
        await asyncio.to_thread(self._provisioning.teardown, name)
