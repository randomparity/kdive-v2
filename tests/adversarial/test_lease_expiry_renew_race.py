"""Adversarial: the ``→expired`` sweep must not clobber a concurrently-renewed lease.

Invariant (ADR-0036 §3-4, `reconciler/loop.py::_expire_one`): the sweep reclaims an
allocation **only** while its lease has actually elapsed (``lease_expiry < now()``). The
sweep selects its candidates in one transaction and then expires each in a *separate*
per-allocation transaction, so a ``renew`` that extends ``lease_expiry`` can commit in the
window between the select and the locked re-read. ``renew`` and ``_expire_one`` serialize
on the ``PROJECT`` advisory lock, so the locked re-read observes a committed renewal — but
the re-read must re-validate the lease window, not only the terminal-state fence (a renew
extends ``lease_expiry`` *without* changing state, so the terminal fence alone misses it).

These tests reproduce the stale-candidate interleaving: a renew lands after candidate
selection, and the sweep must skip the now-live allocation instead of expiring a lease the
project just paid to extend.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg

from kdive.domain.allocation_renew import renew
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.reconciler.loop import _expire_one
from tests.adversarial.conftest import (
    one,
    open_conn,
    open_conns,
    seed_allocation,
    seed_budget,
    seed_quota,
    seed_resource,
)

CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))


async def _seed_lapsed_active(conn: psycopg.AsyncConnection) -> UUID:
    """Seed an ``active`` allocation whose lease has already elapsed (a sweep candidate)."""
    resource = await seed_resource(conn, cap=10)
    await seed_budget(conn)
    await seed_quota(conn)
    alloc = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE allocations SET lease_expiry = now() - interval '1 hour', "
            "active_started_at = now() - interval '2 hours', "
            "requested_vcpus = 1, requested_memory_gb = 0 WHERE id = %s",
            (alloc.id,),
        )
    return alloc.id


async def _state(conn: psycopg.AsyncConnection, alloc_id: UUID) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
        return (await one(cur))[0]


def test_expire_one_skips_a_renewed_lease(migrated_url: str) -> None:
    # Models the real interleaving: the sweep picked this id as a candidate
    # (lease_expiry < now()), then a renew committed a future lease_expiry before
    # _expire_one took the per-allocation lock. The locked re-read must re-validate the
    # lease window and skip — not expire a lease the project just renewed.
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            alloc_id = await _seed_lapsed_active(conn)
            # A renew that landed after candidate selection: the lease is now live.
            await conn.execute(
                "UPDATE allocations SET lease_expiry = now() + interval '1 hour' WHERE id = %s",
                (alloc_id,),
            )
            expired = await _expire_one(conn, alloc_id, "proj")
            assert expired is False, "sweep expired an allocation whose lease was renewed"
            assert await _state(conn, alloc_id) == "active"
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
                )
                assert (await one(cur))[0] is None, "billing interval closed on a live lease"

    asyncio.run(_run())


def test_expire_one_still_expires_a_genuinely_lapsed_lease(migrated_url: str) -> None:
    # The fence must not over-correct: an allocation still past its lease is reclaimed.
    async def _run() -> None:
        async with open_conn(migrated_url) as conn:
            alloc_id = await _seed_lapsed_active(conn)
            expired = await _expire_one(conn, alloc_id, "proj")
            assert expired is True
            assert await _state(conn, alloc_id) == "expired"

    asyncio.run(_run())


def test_renew_durable_against_concurrent_sweep(migrated_url: str) -> None:
    # Genuinely concurrent: race a real renew against the sweep's per-allocation expire on
    # distinct connections (both serialize on the PROJECT lock). Whoever wins, the safety
    # invariant holds: a renewal that succeeded must leave the allocation non-terminal — the
    # reconciler must never expire a lease the project just extended.
    async def _run() -> None:
        for _ in range(24):
            async with open_conn(migrated_url) as seed:
                alloc_id = await _seed_lapsed_active(seed)
            async with open_conns(migrated_url, 2) as (rc, ec):
                renew_outcome, _ = await asyncio.gather(
                    renew(rc, CTX, allocation_id=alloc_id, extend=2),
                    _expire_one(ec, alloc_id, "proj"),
                )
            async with open_conn(migrated_url) as check:
                final = await _state(check, alloc_id)
            if renew_outcome.renewed:
                assert final != "expired", (
                    "renew succeeded but the sweep expired the allocation anyway"
                )

    asyncio.run(_run())
