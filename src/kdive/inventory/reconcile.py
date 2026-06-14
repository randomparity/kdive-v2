"""Inventory reconcile engine core (M2.6 #390, ADR-0112).

Provides the shared merge-contract primitives every per-entity reconciler reuses:

* :class:`ReconcileDiff` — the per-pass outcome (``created`` / ``updated`` / ``pruned`` /
  ``cordoned`` / ``warned`` lists of small records), surfaced to the operator and the loop;
* :data:`ManagedBy` — re-exported from :mod:`kdive.domain.models` (the ownership partition);
* :func:`inventory_pass_lock` — a session-scoped advisory lock held for a **whole** reconcile
  pass, so the multi-transaction pass (batched upsert + per-row prunes) never races a second
  pass into the ``(provider, name, arch)`` identity constraint;
* :func:`prune_or_cordon_image` — the non-destructive prune decision: a base image a
  non-terminal System still references is **cordoned** (left in place, surfaced), never
  deleted; an idle config row's **row is deleted** (never ``store.delete`` inline — ADR-0112
  keeps object reclamation in the existing ``repair_leaked_images`` GC).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row

from kdive.db.locks import INVENTORY_RECONCILE, session_advisory_lock
from kdive.domain.models import ManagedBy
from kdive.services.allocation.pcie_claim import NON_TERMINAL_STATES_VALUES
from kdive.services.images.retention import image_referenced_by_live_system

__all__ = [
    "ManagedBy",
    "ReconcileDiff",
    "ReconcileRecord",
    "PruneOutcome",
    "inventory_pass_lock",
    "prune_or_cordon_image",
    "prune_or_cordon_resource",
    "prune_or_cordon_build_host",
]


@dataclass(frozen=True)
class ReconcileRecord:
    """One reconciled or warned entity, identified for the operator-facing diff.

    Args:
        name: The entity's stable name (an image ``name``).
        entry: A human-readable identity (e.g. ``image[provider/name/arch]``) for warnings.
        detail: An optional short reason (e.g. why a row warned or was cordoned).
    """

    name: str
    entry: str
    detail: str = ""


@dataclass
class ReconcileDiff:
    """The outcome of one reconcile pass, per entity type.

    ``created``/``updated``/``pruned``/``cordoned`` are the change sets; ``warned`` carries
    non-fatal degradations (an ``s3`` row left ``defined`` for a missing digest/object, a
    ``build`` base not yet registered). Steady state is every list empty — a pass that
    re-emits unchanged rows would make the loop report perpetual phantom drift, so the
    per-entity reconcilers only append on a real change (change-detecting upserts).
    """

    created: list[ReconcileRecord] = field(default_factory=list)
    updated: list[ReconcileRecord] = field(default_factory=list)
    pruned: list[ReconcileRecord] = field(default_factory=list)
    cordoned: list[ReconcileRecord] = field(default_factory=list)
    warned: list[ReconcileRecord] = field(default_factory=list)


@dataclass(frozen=True)
class PruneOutcome:
    """The decision :func:`prune_or_cordon_image` reached for one candidate row."""

    pruned: bool
    cordoned: bool


@asynccontextmanager
async def inventory_pass_lock(conn: AsyncConnection) -> AsyncIterator[None]:
    """Hold the session-scoped inventory lock for a whole reconcile pass (ADR-0112).

    A pass spans multiple transactions (the batched upsert, then a per-row prune), so it
    must serialize on a **session**-scoped lock — an xact lock would auto-release at the end
    of the first transaction and let a second concurrent pass race the identity constraint.
    """
    async with session_advisory_lock(conn, INVENTORY_RECONCILE):
        yield


async def prune_or_cordon_image(conn: AsyncConnection, row_id: UUID, name: str) -> PruneOutcome:
    """Apply the non-destructive prune contract to one config image row (ADR-0112).

    Runs in its own transaction (so the liveness re-check and the row delete are atomic, and
    one row's outcome never half-commits another's). The liveness guard reuses the shared,
    now kind-aware :func:`image_referenced_by_live_system`: a base image a non-terminal System
    still references is **cordoned** (left in place) — never deleted. An idle row's **row** is
    deleted; the S3 object (if any) is intentionally left for the existing
    ``repair_leaked_images`` sweep to reclaim once it is rowless past the publish grace.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened here).
        row_id: The config image row's id.
        name: The image name (for the returned record).

    Returns:
        A :class:`PruneOutcome` recording whether the row was pruned or cordoned.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM image_catalog WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, ManagedBy.CONFIG.value),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await image_referenced_by_live_system(cur, row_id):
            return PruneOutcome(pruned=False, cordoned=True)
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def prune_or_cordon_resource(conn: AsyncConnection, row_id: UUID, name: str) -> PruneOutcome:
    """Apply the non-destructive prune contract to one config resource row (ADR-0112).

    Mirrors :func:`prune_or_cordon_image`: a config resource that left the file is **deleted**
    only when it is idle. A resource with a **live** (non-terminal) allocation is **cordoned**
    (``cordoned=true``, stops new placement) and surfaced — never deleted, so a file edit can
    never evict a running System (eviction stays the explicit ``resources.drain`` op). Runs in
    its own transaction so the liveness re-check and the delete/cordon are atomic per row.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened here).
        row_id: The config resource row's id.
        name: The resource name (for the returned record).

    Returns:
        A :class:`PruneOutcome` recording whether the row was pruned or cordoned.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM resources WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, ManagedBy.CONFIG.value),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await _resource_has_live_allocation(cur, row_id):
            await cur.execute(
                "UPDATE resources SET cordoned = true WHERE id = %s AND NOT cordoned", (row_id,)
            )
            return PruneOutcome(pruned=False, cordoned=True)
        await cur.execute("DELETE FROM resources WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def _resource_has_live_allocation(cur: AsyncCursor[dict[str, Any]], row_id: UUID) -> bool:
    """True when the resource backs a non-terminal allocation (the refuse-if-live predicate)."""
    await cur.execute(
        "SELECT 1 FROM allocations WHERE resource_id = %s AND state = ANY(%s) LIMIT 1",
        (row_id, list(NON_TERMINAL_STATES_VALUES)),
    )
    return await cur.fetchone() is not None


async def prune_or_cordon_build_host(
    conn: AsyncConnection, row_id: UUID, name: str
) -> PruneOutcome:
    """Apply the non-destructive prune contract to one config build-host row (ADR-0112).

    Mirrors :func:`prune_or_cordon_resource`, but the build-host "live" predicate is an
    in-flight ``build_host_leases`` row and the cordon mechanism is ``enabled = false`` (the
    build_hosts table has no ``cordoned`` column — a disabled host is skipped by both the
    build-host scheduler and the reachability probe). The DB itself guards prune:
    ``build_host_leases`` FKs ``build_hosts(id) ON DELETE RESTRICT``, so a blind ``DELETE`` of
    a busy host would abort the whole pass. This helper checks the lease **first** (under
    ``FOR UPDATE``) and cordons a busy host instead of ever attempting the aborting delete.

    Args:
        conn: The reconcile pass connection (a fresh transaction is opened here).
        row_id: The config build-host row's id.
        name: The build-host name (for the returned record).

    Returns:
        A :class:`PruneOutcome` recording whether the row was pruned or cordoned.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM build_hosts WHERE id = %s AND managed_by = %s FOR UPDATE",
            (row_id, ManagedBy.CONFIG.value),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await _build_host_has_live_lease(cur, row_id):
            await cur.execute(
                "UPDATE build_hosts SET enabled = false WHERE id = %s AND enabled", (row_id,)
            )
            return PruneOutcome(pruned=False, cordoned=True)
        await cur.execute("DELETE FROM build_hosts WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def _build_host_has_live_lease(cur: AsyncCursor[dict[str, Any]], row_id: UUID) -> bool:
    """True when the build host holds an in-flight capacity lease (the refuse-if-live guard)."""
    await cur.execute(
        "SELECT 1 FROM build_host_leases WHERE build_host_id = %s LIMIT 1",
        (row_id,),
    )
    return await cur.fetchone() is not None
