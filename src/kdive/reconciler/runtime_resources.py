"""Lease + reachability reaping for runtime-registered resources (M2.6 #398, ADR-0112).

Imperative agent tools (``resources.register_*``/``renew``) write ``managed_by='runtime'``
resource rows carrying a ``lease_expires_at`` the agent must renew. This reaper is the
leak backstop: a runtime resource whose lease has lapsed (the agent vanished without
deregistering) is removed so a disappeared agent never leaves permanent shared capacity.

The contract is **cordon-only / refuse-if-live**, identical to the config-prune contract in
:func:`kdive.inventory.reconcile.prune_or_cordon_resource` and to ADR-0109's terminal-state
predicate: a lapsed-lease resource that still backs a **live (non-terminal) allocation** is
**cordoned** (``cordoned=true`` stops new placement) and surfaced — never destroyed, so a
lease expiry can never auto-drain a running System (including a ``crashed`` System under live
crash-debug). Eviction stays the explicit ``resources.drain`` op.

Scope is strictly ``managed_by='runtime'``: ``config`` and ``discovery`` rows carry **no**
lease (``lease_expires_at`` is NULL on them) and are **never** lease-reaped here. The lease IS
the TTL window — a transiently-unreachable but still-leased resource keeps a future
``lease_expires_at`` (the agent is renewing) and is never a candidate, so a reachability flap
cannot reap a host before its lease lapses. When a :class:`ResourceProbe` is injected the
reaper probes each lapsed-lease candidate only to **confirm and log** sustained
unreachability; the probe is never required for a reap (lease expiry alone is sufficient) and
its absence never blocks one.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.models import ManagedBy
from kdive.inventory.reconcile import PruneOutcome
from kdive.services.allocation.pcie_claim import NON_TERMINAL_STATES_VALUES

_log = logging.getLogger(__name__)


@runtime_checkable
class ResourceProbe(Protocol):
    """Report whether a resource host is reachable (the reachability-confirmation port).

    Structurally identical to ``resources.register_*``'s probe port so the same
    ``TcpResourceProbe`` satisfies both without the reconciler importing the mcp layer (a
    lower layer must not depend on a higher one). The reaper uses it only to log whether a
    lapsed-lease host is also unreachable; reaping never depends on the probe.
    """

    async def probe(self, host_uri: str) -> bool: ...


_RUNTIME = ManagedBy.RUNTIME.value


async def reap_expired_runtime_resources(
    conn: AsyncConnection, probe: ResourceProbe | None = None
) -> int:
    """Reap (or cordon) runtime resources whose lease has lapsed (ADR-0112).

    The candidate set is every ``managed_by='runtime'`` resource whose ``lease_expires_at``
    is in the past (the lease-expiry trigger; the lease IS the TTL, so this is also the
    "sustained unreachability past TTL" signal). Each candidate is removed when idle, or
    **cordoned** when it still backs a live allocation (refuse-if-live — never auto-drain).

    The candidate ids are read in a committed transaction first, so no transaction is held
    open across the (optional) per-candidate network probe. When ``probe`` is supplied each
    candidate's ``host_uri`` is probed only to log whether the lapsed-lease host is also
    unreachable; the probe outcome never changes the reap decision (lease expiry is
    sufficient) and a probe error never starves the pass.

    Args:
        conn: A fresh, transaction-free pooled connection.
        probe: Optional reachability probe; when present, confirms/logs unreachability.

    Returns:
        The number of resources reaped (pruned) **or** cordoned this pass (steady state 0).
    """
    candidates = await _lapsed_lease_candidates(conn)
    if not candidates:
        return 0

    acted = 0
    for row_id, host_uri in candidates:
        try:
            if probe is not None and host_uri:
                await _log_reachability(probe, row_id, host_uri)
            outcome = await _prune_or_cordon_runtime_resource(conn, row_id)
        except Exception:  # noqa: BLE001 - isolate one candidate; one failure must not starve the rest
            _log.warning(
                "reconciler: reaping runtime resource %s failed this pass",
                row_id,
                exc_info=True,
            )
            continue
        if outcome.pruned:
            acted += 1
            _log.info("reconciler: runtime resource %s lease lapsed; row reaped", row_id)
        elif outcome.cordoned:
            acted += 1
            _log.info(
                "reconciler: runtime resource %s lease lapsed but in use; cordoned (not reaped)",
                row_id,
            )
    return acted


async def _lapsed_lease_candidates(conn: AsyncConnection) -> list[tuple[UUID, str]]:
    """The ``(id, host_uri)`` of every runtime resource whose lease is in the past (DB clock)."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, host_uri FROM resources "
            "WHERE managed_by = %s AND lease_expires_at IS NOT NULL AND lease_expires_at < now()",
            (_RUNTIME,),
        )
        rows = await cur.fetchall()
    return [(_row_id(row), str(row["host_uri"])) for row in rows]


async def _prune_or_cordon_runtime_resource(conn: AsyncConnection, row_id: UUID) -> PruneOutcome:
    """Delete an idle lapsed-lease runtime resource; cordon one with a live allocation.

    Mirrors :func:`kdive.inventory.reconcile.prune_or_cordon_resource` but scoped to
    ``managed_by='runtime'`` and re-checking the lease under ``FOR UPDATE`` so a concurrent
    ``resources.renew`` that lands between the candidate read and this transaction wins (the
    re-check sees the extended lease and the row is left alone). Runs in its own transaction so
    the liveness/lease re-check and the delete/cordon are atomic per row.

    The cordon is **change-detecting**: a lapsed-lease + live resource stays a candidate every
    pass, so ``cordoned`` is reported ``True`` only on the pass that actually flips the flag
    (``cur.rowcount == 1``). An already-cordoned row is a no-op (``pruned=cordoned=False``) so
    the steady-state reap count returns to ``0`` instead of reporting perpetual phantom drift.
    """
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM resources "
            "WHERE id = %s AND managed_by = %s "
            "  AND lease_expires_at IS NOT NULL AND lease_expires_at < now() "
            "FOR UPDATE",
            (row_id, _RUNTIME),
        )
        if await cur.fetchone() is None:
            return PruneOutcome(pruned=False, cordoned=False)
        if await _resource_has_live_allocation(cur, row_id):
            await cur.execute(
                "UPDATE resources SET cordoned = true WHERE id = %s AND NOT cordoned", (row_id,)
            )
            return PruneOutcome(pruned=False, cordoned=cur.rowcount == 1)
        await cur.execute("DELETE FROM resources WHERE id = %s", (row_id,))
    return PruneOutcome(pruned=True, cordoned=False)


async def _resource_has_live_allocation(cur: Any, row_id: UUID) -> bool:
    """True when the resource backs a non-terminal allocation (the refuse-if-live predicate)."""
    await cur.execute(
        "SELECT 1 FROM allocations WHERE resource_id = %s AND state = ANY(%s) LIMIT 1",
        (row_id, list(NON_TERMINAL_STATES_VALUES)),
    )
    return await cur.fetchone() is not None


async def _log_reachability(probe: ResourceProbe, row_id: UUID, host_uri: str) -> None:
    """Probe ``host_uri`` only to log whether a lapsed-lease host is also unreachable."""
    reachable = await probe.probe(host_uri)
    if not reachable:
        _log.info(
            "reconciler: lapsed-lease runtime resource %s host %r is also unreachable",
            row_id,
            host_uri,
        )


def _row_id(row: dict[str, Any]) -> UUID:
    value = row["id"]
    assert isinstance(value, UUID)
    return value


__all__ = ["ResourceProbe", "reap_expired_runtime_resources"]
