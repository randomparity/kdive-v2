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
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import INVENTORY_RECONCILE, session_advisory_lock
from kdive.domain.models import ManagedBy
from kdive.services.images.retention import image_referenced_by_live_system

__all__ = [
    "ManagedBy",
    "ReconcileDiff",
    "ReconcileRecord",
    "PruneOutcome",
    "inventory_pass_lock",
    "prune_or_cordon_image",
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
