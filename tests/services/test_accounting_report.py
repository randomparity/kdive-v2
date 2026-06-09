"""accounting.report domain rollup tests (ADR-0043 §3, issue #97).

`report` aggregates the signed ledger over an already-authorized project set, returning
per-project (or per-(project, principal)) `{reserved, reconciled, variance}` plus a
cross-project total. The domain layer does no authorization — it sums the set it is
handed. These tests drive it directly on an injected connection against real Postgres,
inserting ledger rows with explicit `event_type`, `principal` (via the owning
allocation), and `ts` so each rollup matches a hand-computed expectation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.services.accounting import ledger as accounting

_DT = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _resource(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return res.id


async def _alloc(
    conn: psycopg.AsyncConnection, resource_id: UUID, *, project: str, principal: str
) -> UUID:
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal=principal,
            project=project,
            resource_id=resource_id,
            state=AllocationState.ACTIVE,
            requested_vcpus=2,
            requested_memory_gb=4,
        ),
    )
    return alloc.id


async def _ledger(
    conn: psycopg.AsyncConnection,
    project: str,
    allocation_id: UUID,
    event_type: str,
    kcu_delta: str,
    ts: datetime = _DT,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ledger (id, ts, project, allocation_id, cost_class, "
            "event_type, kcu_delta) VALUES (%s, %s, %s, %s, 'local', %s, %s)",
            (uuid4(), ts, project, allocation_id, event_type, Decimal(kcu_delta)),
        )


def _row(report: accounting.Report, project: str, principal: str | None = None):
    for row in report.rows:
        if row.project == project and row.principal == principal:
            return row
    raise AssertionError(f"no row for ({project!r}, {principal!r}) in {report.rows!r}")


def test_report_multi_project_per_project_and_total_variance(migrated_url: str) -> None:
    # Hand-computed ledger across two projects:
    #   A: reserved +10, reconciled -3  → reserved=10 reconciled=-3 variance=-13
    #   B: reserved +20, reconciled +5  → reserved=20 reconciled=+5 variance=-15
    #   total: reserved=30 reconciled=2 variance=-28
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _resource(conn)
            a = await _alloc(conn, res, project="proj-a", principal="alice")
            b = await _alloc(conn, res, project="proj-b", principal="bob")
            await _ledger(conn, "proj-a", a, "reserved", "10")
            await _ledger(conn, "proj-a", a, "reconciled", "-3")
            await _ledger(conn, "proj-b", b, "reserved", "20")
            await _ledger(conn, "proj-b", b, "reconciled", "5")

            report = await accounting.report(
                conn, projects=["proj-a", "proj-b"], group_by=None, window=None
            )

        a_row = _row(report, "proj-a")
        assert (a_row.reserved, a_row.reconciled, a_row.variance) == (
            Decimal("10.0000"),
            Decimal("-3.0000"),
            Decimal("-13.0000"),
        )
        b_row = _row(report, "proj-b")
        assert (b_row.reserved, b_row.reconciled, b_row.variance) == (
            Decimal("20.0000"),
            Decimal("5.0000"),
            Decimal("-15.0000"),
        )
        assert report.total.project == "*"
        assert report.total.principal is None
        assert (report.total.reserved, report.total.reconciled, report.total.variance) == (
            Decimal("30.0000"),
            Decimal("2.0000"),
            Decimal("-28.0000"),
        )

    asyncio.run(_run())


def test_report_group_by_principal_via_ledger_join_allocations(migrated_url: str) -> None:
    # principal comes from ledger ⋈ allocations on allocation_id. Within project A:
    #   alice: reserved +10 reconciled -2  → variance -12
    #   bob:   reserved +4                 → variance -4
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _resource(conn)
            a_alice = await _alloc(conn, res, project="proj-a", principal="alice")
            a_bob = await _alloc(conn, res, project="proj-a", principal="bob")
            await _ledger(conn, "proj-a", a_alice, "reserved", "10")
            await _ledger(conn, "proj-a", a_alice, "reconciled", "-2")
            await _ledger(conn, "proj-a", a_bob, "reserved", "4")

            report = await accounting.report(
                conn, projects=["proj-a"], group_by="principal", window=None
            )

        alice = _row(report, "proj-a", "alice")
        assert (alice.reserved, alice.reconciled, alice.variance) == (
            Decimal("10.0000"),
            Decimal("-2.0000"),
            Decimal("-12.0000"),
        )
        bob = _row(report, "proj-a", "bob")
        assert (bob.reserved, bob.reconciled, bob.variance) == (
            Decimal("4.0000"),
            Decimal("0.0000"),
            Decimal("-4.0000"),
        )
        # Total is cross-principal: principal stays None even when grouped.
        assert report.total.principal is None
        assert report.total.reserved == Decimal("14.0000")

    asyncio.run(_run())


def test_report_window_bounds_on_ledger_ts(migrated_url: str) -> None:
    # Half-open [start, end): rows at ts < start or ts >= end are excluded.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _resource(conn)
            a = await _alloc(conn, res, project="proj-a", principal="alice")
            before = datetime(2026, 1, 1, tzinfo=UTC)
            inside = datetime(2026, 1, 15, tzinfo=UTC)
            at_end = datetime(2026, 2, 1, tzinfo=UTC)
            await _ledger(conn, "proj-a", a, "reserved", "100", before)
            await _ledger(conn, "proj-a", a, "reserved", "7", inside)
            await _ledger(conn, "proj-a", a, "reserved", "100", at_end)

            start = datetime(2026, 1, 10, tzinfo=UTC)
            end = datetime(2026, 2, 1, tzinfo=UTC)
            report = await accounting.report(
                conn, projects=["proj-a"], group_by=None, window=(start, end)
            )

        assert _row(report, "proj-a").reserved == Decimal("7.0000")

    asyncio.run(_run())


def test_report_empty_project_set_is_empty_report(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            report = await accounting.report(conn, projects=[], group_by=None, window=None)
        assert report.rows == ()
        assert report.total.reserved == Decimal("0.0000")
        assert report.total.reconciled == Decimal("0.0000")
        assert report.total.variance == Decimal("0.0000")

    asyncio.run(_run())


def test_report_project_with_no_ledger_rows_contributes_no_row(migrated_url: str) -> None:
    # proj-b is authorized but has no ledger rows: it yields no RollupRow, and the
    # total reflects only proj-a.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _resource(conn)
            a = await _alloc(conn, res, project="proj-a", principal="alice")
            await _ledger(conn, "proj-a", a, "reserved", "5")
            report = await accounting.report(
                conn, projects=["proj-a", "proj-b"], group_by=None, window=None
            )
        assert {r.project for r in report.rows} == {"proj-a"}
        assert report.total.reserved == Decimal("5.0000")

    asyncio.run(_run())
