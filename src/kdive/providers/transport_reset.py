"""The reconciler's provider port for resetting a dead session's transport (#216, ADR-0086).

When the reconciler detaches a stale ``live`` DebugSession whose worker died, a remote
provider's single-client gdbstub can still be held by the dead worker's lingering TCP
connection (ADR-0079). This narrow port lets the reconciler reset that transport without
importing a provider — mirroring :mod:`kdive.providers.reaping`. ``NullResetter`` is the
default (local-libvirt's co-located gdbstub is freed by the host OS on worker death).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TransportResetter(Protocol):
    """Reset a detached dead session's transport so its port stops blocking re-attach.

    The reconciler passes only core-available data; the concrete resetter self-selects the
    sessions it owns (e.g. remote gdbstub) and no-ops the rest.
    """

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None: ...


class NullResetter:
    """The default resetter: touches no transport (local-libvirt needs no active reset)."""

    async def reset(
        self, *, transport: str, transport_handle: str | None, domain_name: str | None
    ) -> None:
        return None
