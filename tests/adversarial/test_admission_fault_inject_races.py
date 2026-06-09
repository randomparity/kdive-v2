"""Adversarial: admission-control concurrency races against the fault-inject resource.

M1.5 issue 6 (spec §Validation surface, ADR-0040). `test_admission_concurrency.py` already
proves the per-resource **capacity** lock never overshoots the host cap for a local-libvirt
resource with a generous budget. This module fills the gaps that suite leaves:

  * it targets the **fault-inject** Resource kind (#185 names it explicitly);
  * it proves the per-project **budget** check-then-debit is atomic under contention — that
    no number of concurrent `admit()` calls can drive ``spent_kcu`` past ``limit_kcu`` (an
    overspend the generous-budget suite never exercises);
  * it forces the racers to genuinely overlap inside the locked critical section with an
    injected per-call latency, so a regression that shrank the lock's scope would fail
    deterministically rather than pass by luck.

The "injected latency / slow release" the spec calls for is the
`test_idempotency_concurrency.py` test-side pattern: an `asyncio.sleep` monkeypatched into a
function the critical section calls, widening the held-lock window. The advisory lock already
serializes the racers, so a **per-call sleep** (never a racer-sized barrier — that would
self-deadlock the lock holder) is what makes a non-atomic check-then-debit lose. The
``unlocked`` falsification test proves that same sleep genuinely exposes the race when no
PROJECT lock guards the check-then-debit.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
import pytest

import kdive.services.allocation_admission as admission
from kdive.domain.errors import ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.services.allocation_admission import AllocationRequest, admit
from kdive.services.allocation_idempotency import within_budget
from tests.adversarial.conftest import (
    SMALL_SELECTOR,
    count_rows,
    open_conn,
    open_conns,
    seed_budget,
    seed_fault_inject_resource,
    seed_quota,
)

if TYPE_CHECKING:
    from kdive.domain.models import Resource

# The estimate one SMALL_SELECTOR (1 vcpu, 0 GB) prices to over a 1-hour window at the
# seeded local coeff (1.0): rate 1.0 kcu/hr × 1 hr = 1 kcu. The budget tests size
# ``limit_kcu`` as an integer multiple of this so "exactly B grants" is exact.
_ESTIMATE = Decimal(1)

# How long each racer dwells inside the patched check, holding the lock so its peers pile up
# on the advisory lock. Small but non-zero — enough to force overlap, fast in CI.
_HOLD_S = 0.05


def _admit(conn: psycopg.AsyncConnection, resource: Resource, *, project: str = "proj"):  # type: ignore[no-untyped-def]
    """Admit one keyless small request against ``resource`` for ``project``.

    ``ctx.projects`` carries ``project`` so the per-project budget/quota the admission gate
    reads are the ones seeded for it (the race tests vary the project to isolate which lock
    serializes the racers).
    """
    return admit(
        conn,
        AllocationRequest(
            ctx=RequestContext(principal="alice", agent_session="s", projects=(project,)),
            resource=resource,
            project=project,
            selector=SMALL_SELECTOR,
            window=1,
        ),
    )


def _slow_within_budget(
    delay: float,
) -> Callable[[psycopg.AsyncConnection, str, Decimal], Awaitable[bool]]:
    """Wrap ``within_budget`` so each call dwells ``delay`` seconds inside the locked check.

    The gate calls ``within_budget`` after acquiring both the PROJECT and RESOURCE locks, so
    the dwell widens whichever lock-held window the test is probing. With the lock the racers
    serialize (the dwell is harmless); without it the racers all read the same pre-debit state
    during the dwell and over-grant — the race each invariant test catches.
    """

    async def _patched(conn: psycopg.AsyncConnection, project: str, estimate: Decimal) -> bool:
        await asyncio.sleep(delay)
        return await within_budget(conn, project, estimate)

    return _patched


async def _spent_kcu(conn: psycopg.AsyncConnection, project: str) -> Decimal:
    """Read the project's running ``spent_kcu`` total."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT spent_kcu FROM budgets WHERE project = %s", (project,))
        row = await cur.fetchone()
    assert row is not None
    return Decimal(row[0])


def test_concurrent_request_no_budget_overspend(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Budget affords exactly B of the racers; host caps generous so budget is the only binding
    # constraint. Each racer targets a DISTINCT fault-inject resource in the SAME project, so
    # the per-resource RESOURCE lock does NOT serialize them — only the per-project budget lock
    # (PROJECT) guards spent_kcu. This isolates the budget lock: under it the check-then-debit
    # is atomic and exactly B grant; without it the racers would overspend (the unlocked-control
    # test proves the dwell exposes that race). One resource per racer keeps capacity slack.
    budget_admits = 3
    racers = 10
    monkeypatch.setattr(admission, "within_budget", _slow_within_budget(_HOLD_S))

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resources = [await seed_fault_inject_resource(seed, cap=1_000) for _ in range(racers)]
            await seed_budget(seed, limit=str(budget_admits))  # B * 1-kcu estimate
            await seed_quota(seed)
        async with open_conns(migrated_url, racers) as conns:
            outcomes = await asyncio.gather(
                *(_admit(c, r) for c, r in zip(conns, resources, strict=True))
            )
        granted = [o for o in outcomes if o.granted]
        denied = [o for o in outcomes if not o.granted]
        assert len(granted) == budget_admits, f"expected {budget_admits} grants, got {len(granted)}"
        assert len(denied) == racers - budget_admits
        assert all(o.category is ErrorCategory.ALLOCATION_DENIED for o in denied)
        async with open_conn(migrated_url) as check:
            spent = await _spent_kcu(check, "proj")
            assert spent == budget_admits * _ESTIMATE, f"spent_kcu drifted: {spent}"
            assert spent <= Decimal(budget_admits), "budget overspent under contention"
            assert await count_rows(check, "allocations") == budget_admits
            assert await count_rows(check, "audit_log") == budget_admits

    asyncio.run(_run())


def test_concurrent_request_budget_exact_boundary(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # limit_kcu == racers * estimate: every racer fits exactly, none spuriously denied, and
    # spent_kcu lands on the limit (the boundary is inclusive: remaining >= estimate). Distinct
    # resources per racer so the budget lock — not the per-resource lock — is what serializes.
    racers = 6
    monkeypatch.setattr(admission, "within_budget", _slow_within_budget(_HOLD_S))

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resources = [await seed_fault_inject_resource(seed, cap=1_000) for _ in range(racers)]
            await seed_budget(seed, limit=str(racers))
            await seed_quota(seed)
        async with open_conns(migrated_url, racers) as conns:
            outcomes = await asyncio.gather(
                *(_admit(c, r) for c, r in zip(conns, resources, strict=True))
            )
        assert all(o.granted for o in outcomes), "a racer was denied at the exact budget boundary"
        async with open_conn(migrated_url) as check:
            assert await _spent_kcu(check, "proj") == Decimal(racers)
            assert await count_rows(check, "allocations") == racers

    asyncio.run(_run())


def test_concurrent_request_no_capacity_double_book(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Host cap K < racers on ONE shared fault-inject resource, with each racer in a DISTINCT
    # project (its own generous budget/quota). Distinct projects mean no shared PROJECT lock —
    # only the per-resource capacity lock (RESOURCE) serializes the count-then-insert, so this
    # isolates the capacity lock the way the budget test isolates the budget lock. Exactly K
    # grant; no double-book.
    cap = 4
    racers = 12
    monkeypatch.setattr(admission, "within_budget", _slow_within_budget(_HOLD_S))
    projects = [f"proj-{i}" for i in range(racers)]

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resource = await seed_fault_inject_resource(seed, cap=cap)
            for project in projects:
                await seed_budget(seed, project=project)
                await seed_quota(seed, project=project)
        async with open_conns(migrated_url, racers) as conns:
            outcomes = await asyncio.gather(
                *(_admit(c, resource, project=p) for c, p in zip(conns, projects, strict=True))
            )
        granted = [o for o in outcomes if o.granted]
        denied = [o for o in outcomes if not o.granted]
        assert len(granted) == cap, f"expected {cap} grants, got {len(granted)}"
        assert all(o.reason == "at_capacity" for o in denied)
        async with open_conn(migrated_url) as check:
            assert await count_rows(check, "allocations") == cap

    asyncio.run(_run())


def test_concurrent_request_both_locks_contended(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BOTH the capacity cap (K) and the budget (affords B) bind, with K != B. The binding
    # minimum grants; neither invariant is violated and the PROJECT -> RESOURCE order never
    # deadlocks.
    cap = 5
    budget_admits = 2
    racers = 14
    monkeypatch.setattr(admission, "within_budget", _slow_within_budget(_HOLD_S))

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            resource = await seed_fault_inject_resource(seed, cap=cap)
            await seed_budget(seed, limit=str(budget_admits))
            await seed_quota(seed)
        async with open_conns(migrated_url, racers) as conns:
            outcomes = await asyncio.gather(*(_admit(c, resource) for c in conns))
        granted = sum(o.granted for o in outcomes)
        assert granted == min(cap, budget_admits), f"expected {min(cap, budget_admits)} grants"
        async with open_conn(migrated_url) as check:
            assert await _spent_kcu(check, "proj") <= Decimal(budget_admits), "budget overspent"
            assert await count_rows(check, "allocations") <= cap, "capacity double-booked"

    asyncio.run(_run())


def test_unlocked_check_then_debit_overspends_under_overlap(migrated_url: str) -> None:
    # Falsification control (mirrors test_idempotency_concurrency's bare-vs-locked pair): the
    # SAME check-then-debit WITHOUT the PROJECT lock, barrier-forced to overlap, DOES
    # overspend. This proves the injected dwell genuinely exposes the race, so the locked
    # tests above pass because of the lock, not a too-fast critical section.
    racers = 4

    async def _unlocked_grant(conn: psycopg.AsyncConnection, barrier: asyncio.Barrier) -> bool:
        # No advisory lock: read remaining, rendezvous so every racer is past its check, then
        # debit if it looked affordable. A lock-free check-then-debit at READ COMMITTED.
        affordable = await within_budget(conn, "proj", _ESTIMATE)
        await barrier.wait()
        if affordable:
            await conn.execute(
                "UPDATE budgets SET spent_kcu = spent_kcu + %s WHERE project = %s",
                (_ESTIMATE, "proj"),
            )
        return affordable

    async def _run() -> None:
        async with open_conn(migrated_url) as seed:
            await seed_budget(seed, limit="1")  # affords exactly one
        barrier = asyncio.Barrier(racers)
        async with open_conns(migrated_url, racers) as conns:
            granted = await asyncio.gather(*(_unlocked_grant(c, barrier) for c in conns))
        assert sum(granted) > 1, "the lock-free control did NOT overspend — the dwell is inert"
        async with open_conn(migrated_url) as check:
            assert await _spent_kcu(check, "proj") > Decimal(1), "spent_kcu did not overshoot"

    asyncio.run(_run())
