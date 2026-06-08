"""Per-kind provider runtime registry (ADR-0071).

The resolver maps a ``ResourceKind`` to the ``ProviderRuntime`` that serves it.
Post-System worker ops resolve their runtime from the System's Resource kind
(``job -> system -> allocation -> resource.kind``); an unregistered kind fails
closed with ``configuration_error`` rather than falling through to a default.
Concrete runtimes are still constructed only in :mod:`kdive.providers.composition`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.runtime import ProviderRuntime

_KIND_FOR_SYSTEM: LiteralString = (
    "SELECT r.kind AS kind FROM systems s "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE s.id = %s"
)
_KIND_FOR_RUN: LiteralString = (
    "SELECT r.kind AS kind FROM runs rn "
    "JOIN systems s ON s.id = rn.system_id "
    "JOIN allocations a ON a.id = s.allocation_id "
    "JOIN resources r ON r.id = a.resource_id "
    "WHERE rn.id = %s"
)


class ProviderResolver:
    """A static ``ResourceKind -> ProviderRuntime`` registry.

    Built per deployment by :func:`kdive.providers.composition.build_provider_resolver`.
    Selection is exhaustive and fail-closed: an unregistered kind raises
    ``configuration_error`` at resolution.
    """

    def __init__(self, runtimes: Mapping[ResourceKind, ProviderRuntime]) -> None:
        if not runtimes:
            raise ValueError("ProviderResolver requires at least one registered runtime")
        self._runtimes: dict[ResourceKind, ProviderRuntime] = dict(runtimes)

    def resolve(self, kind: ResourceKind) -> ProviderRuntime:
        """Return the runtime registered for ``kind`` or fail closed."""
        runtime = self._runtimes.get(kind)
        if runtime is None:
            raise CategorizedError(
                f"no provider runtime is registered for resource kind {kind.value!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "kind": kind.value,
                    "registered": sorted(k.value for k in self._runtimes),
                },
            )
        return runtime

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return frozenset(self._runtimes)

    def runtimes(self) -> tuple[ProviderRuntime, ...]:
        return tuple(self._runtimes.values())

    async def register_all_discovery(self, pool: AsyncConnectionPool) -> None:
        """Run every composed runtime's discovery registrar.

        Discovery keys on the map entry's own kind, not on a Resource that does
        not yet exist (ADR-0071), so it fans out over the composed runtimes.
        """
        for runtime in self._runtimes.values():
            await runtime.register_discovery(pool)

    async def runtime_for_system(self, conn: AsyncConnection, system_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_SYSTEM, system_id, "system"))

    async def runtime_for_run(self, conn: AsyncConnection, run_id: UUID) -> ProviderRuntime:
        return self.resolve(await self._kind(conn, _KIND_FOR_RUN, run_id, "run"))

    async def _kind(
        self, conn: AsyncConnection, sql: LiteralString, object_id: UUID, object_kind: str
    ) -> ResourceKind:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (object_id,))
            row = await cur.fetchone()
        if row is None:
            raise CategorizedError(
                f"cannot resolve a provider runtime: no resource kind for {object_kind}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={object_kind: str(object_id)},
            )
        return ResourceKind(row["kind"])
