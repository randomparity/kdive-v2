"""Adversarial: concurrent allocation admission must never overshoot the cap.

Invariant (ADR-0023, `services/allocation_admission.py`): the per-resource advisory
lock serializes count-then-insert, so for a host with cap K, no number of
*genuinely concurrent* admit() calls on *distinct* connections can leave more than
K non-terminal allocations. The existing suite only proves this sequentially on a
single connection; these tests race separate connections to attack the real lock.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.models import Resource
from kdive.domain.state import AllocationState
from kdive.mcp.auth import RequestContext
from kdive.services.allocation_admission import AllocationRequest, admit
from tests.adversarial.conftest import (
    SMALL_SELECTOR,
    count_rows,
    open_conn,
    open_conns,
    seed_budget,
    seed_quota,
    seed_resource,
)

CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))


def _admit(conn: psycopg.AsyncConnection, resource: Resource):  # type: ignore[no-untyped-def]
    # The host cap is the binding constraint here; budget/quota are seeded generous so
    # admission's per-project checks never deny. Each racer is keyless (a distinct grant).
    return admit(
        conn,
        AllocationRequest(
            ctx=CTX,
            resource=resource,
            project="proj",
            selector=SMALL_SELECTOR,
            window=1,
        ),
    )


@pytest.mark.parametrize(("cap", "racers"), [(1, 8), (3, 12), (5, 20)])
def test_concurrent_admit_never_overshoots_cap(migrated_url: str, cap: int, racers: int) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resource = await seed_resource(seed, cap=cap)
            await seed_budget(seed)
            await seed_quota(seed)
        async with open_conns(migrated_url, racers) as conns:
            outcomes = await asyncio.gather(*(_admit(c, resource) for c in conns))
        granted = [o for o in outcomes if o.granted]
        denied = [o for o in outcomes if not o.granted]
        assert len(granted) == cap, f"expected exactly {cap} grants, got {len(granted)}"
        assert len(denied) == racers - cap
        assert all(o.reason == "at_capacity" for o in denied)
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "allocations") == cap
            assert await count_rows(check, "audit_log") == cap  # one audit per grant only

    asyncio.run(_run())


def test_concurrent_admit_at_exact_capacity_all_grant(migrated_url: str) -> None:
    # racers == cap: every concurrent caller must win a slot, none spuriously denied.
    cap = 6

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resource = await seed_resource(seed, cap=cap)
            await seed_budget(seed)
            await seed_quota(seed)
        async with open_conns(migrated_url, cap) as conns:
            outcomes = await asyncio.gather(*(_admit(c, resource) for c in conns))
        assert all(o.granted for o in outcomes)
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "allocations") == cap

    asyncio.run(_run())


def test_concurrent_admit_cap_zero_denies_all(migrated_url: str) -> None:
    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resource = await seed_resource(seed, cap=0)
            await seed_budget(seed)
            await seed_quota(seed)
        async with open_conns(migrated_url, 8) as conns:
            outcomes = await asyncio.gather(*(_admit(c, resource) for c in conns))
        assert all(not o.granted for o in outcomes)
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "allocations") == 0

    asyncio.run(_run())


def test_concurrent_admit_counts_existing_non_terminal(migrated_url: str) -> None:
    # Pre-seed K-1 active allocations; only one of N racers may take the last slot.
    cap = 4
    preexisting = 3

    async def _run() -> None:
        from tests.adversarial.conftest import seed_allocation

        async with open_conn(migrated_url) as seed:
            resource = await seed_resource(seed, cap=cap)
            await seed_budget(seed)
            await seed_quota(seed)
            for _ in range(preexisting):
                await seed_allocation(seed, resource.id, AllocationState.ACTIVE)
        async with open_conns(migrated_url, 10) as conns:
            outcomes = await asyncio.gather(*(_admit(c, resource) for c in conns))
        assert sum(o.granted for o in outcomes) == cap - preexisting
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "allocations") == cap

    asyncio.run(_run())
