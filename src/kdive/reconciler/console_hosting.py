"""Reconciler-owned console-hosting database adapters (ADR-0095)."""

from __future__ import annotations

from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ResourceKind
from kdive.domain.state import SystemState

# A remote System in one of these states has a live domain whose console should be streamed.
# Terminal states (torn_down, failed) and pre-domain states (defined, provisioning) are excluded.
_RUNNING_SYSTEM_STATE_VALUES = (
    SystemState.READY.value,
    SystemState.REPROVISIONING.value,
    SystemState.CRASHED.value,
)
_REMOTE_KIND_VALUE = ResourceKind.REMOTE_LIBVIRT.value


class DbRunningRemoteSystems:
    """Production :class:`RunningSystems`: the running remote Systems from Postgres.

    Selects remote-libvirt Systems with a live domain (state ready/reprovisioning/crashed and a
    non-null ``domain_name``) on a fresh pooled connection per call.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_running(self) -> set[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT s.id FROM systems s "
                "JOIN allocations a ON a.id = s.allocation_id "
                "JOIN resources r ON r.id = a.resource_id "
                "WHERE r.kind = %s AND s.state = ANY(%s) AND s.domain_name IS NOT NULL",
                (_REMOTE_KIND_VALUE, list(_RUNNING_SYSTEM_STATE_VALUES)),
            )
            return {row[0] for row in await cur.fetchall()}
