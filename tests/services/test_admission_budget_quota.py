"""Budget/quota admission-gate tests (ADR-0007 §4-6, ADR-0040). Real Postgres.

`admit` is called directly on an injected autocommit connection with seeded
budget/quota/resource rows. These cover the M1 invariants the M0 host-cap tests do not:
the per-project quota and budget checks, the reserve-at-grant debit, request
idempotency, and the all-or-nothing denial (no row on any failing check).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from kdive.db.repositories import BUDGETS, QUOTAS, RESOURCES
from kdive.domain.cost import Selector
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Budget, Quota, Resource, ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.services.allocation import idempotency as allocation_idempotency
from kdive.services.allocation.admission import (
    AllocationRequest,
    admit,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)
CTX = RequestContext(principal="alice", agent_session="s", projects=("proj",))
SEL = Selector(vcpus=2, memory_gb=4, cost_class="local")
# coeff(local)=1.0; rate = 1.0*(1.0*2 + 0.25*4) = 3.0 kcu/hr.


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(
    conn: psycopg.AsyncConnection, *, cap: int = 5, vcpus: int = 64, memory_mb: int = 65536
) -> Resource:
    return await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: cap,
                "vcpus": vcpus,
                "memory_mb": memory_mb,
            },
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )


async def _seed_budget(conn: psycopg.AsyncConnection, *, limit: str) -> None:
    await BUDGETS.upsert(
        conn, Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT)
    )


async def _seed_quota(
    conn: psycopg.AsyncConnection, *, allocs: int = 10, systems: int = 10
) -> None:
    await QUOTAS.upsert(
        conn,
        Quota(
            project="proj",
            max_concurrent_allocations=allocs,
            max_concurrent_systems=systems,
            updated_at=_DT,
        ),
    )


async def _spent(conn: psycopg.AsyncConnection) -> Decimal:
    budget = await BUDGETS.get(conn, "proj")
    assert budget is not None
    return budget.spent_kcu


async def _count(conn: psycopg.AsyncConnection, table: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _admit(conn: psycopg.AsyncConnection, **kw: object):  # type: ignore[no-untyped-def]
    return admit(
        conn,
        AllocationRequest(
            ctx=CTX,
            resource=kw.pop("resource"),  # ty: ignore[invalid-argument-type]
            project="proj",
            selector=kw.pop("selector", SEL),  # ty: ignore[invalid-argument-type]
            window=kw.pop("window", 2),
            idempotency_key=kw.pop("idempotency_key", None),  # ty: ignore[invalid-argument-type]
        ),
    )


def test_grant_reserves_estimate_and_writes_one_ledger_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is True
            assert outcome.allocation is not None
            alloc = outcome.allocation
            assert alloc.state is AllocationState.GRANTED
            assert alloc.requested_vcpus == 2 and alloc.requested_memory_gb == 4
            assert alloc.active_started_at is None
            assert alloc.lease_expiry is not None
            # rate 3.0 × window 2h = 6.0000 reserved.
            assert await _spent(conn) == Decimal("6.0000")
            assert await _count(conn, "ledger") == 1
            assert await _count(conn, "allocations") == 1
            # one ->granted admission audit row.
            assert await _count(conn, "audit_log") == 1

    asyncio.run(_run())


def test_within_budget_is_false_without_budget_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            assert await allocation_idempotency.within_budget(conn, "proj", Decimal("1")) is False

    asyncio.run(_run())


def test_within_budget_compares_remaining_budget(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_budget(conn, limit="10")
            await conn.execute("UPDATE budgets SET spent_kcu = %s WHERE project = %s", (6, "proj"))

            exact = await allocation_idempotency.within_budget(conn, "proj", Decimal("4"))
            too_much = await allocation_idempotency.within_budget(conn, "proj", Decimal("4.0001"))

        assert exact is True
        assert too_much is False

    asyncio.run(_run())


def test_over_budget_denies_with_no_rows(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="5")  # estimate 6.0 > 5
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0
            assert await _count(conn, "audit_log") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_exactly_at_budget_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="6")  # estimate 6.0 == remaining 6
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is True
            assert await _spent(conn) == Decimal("6.0000")

    asyncio.run(_run())


def test_no_budget_row_denies_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_quota(conn)  # quota present, budget absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.ALLOCATION_DENIED
            assert await _count(conn, "allocations") == 0

    asyncio.run(_run())


def test_no_quota_row_denies_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")  # budget present, quota absent
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.QUOTA_EXCEEDED
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())


def test_at_alloc_quota_denies_quota_exceeded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=1)
            first = await _admit(conn, resource=res, idempotency_key=None)
            assert first.granted is True
            second = await _admit(conn, resource=res, idempotency_key=None)
            assert second.granted is False
            assert second.category is ErrorCategory.QUOTA_EXCEEDED
            assert await _count(conn, "allocations") == 1  # second wrote nothing
            assert await _spent(conn) == Decimal("6.0000")  # only the first reserved

    asyncio.run(_run())


def test_over_caps_selector_is_config_error_no_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, vcpus=2, memory_mb=4096)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res, selector=Selector(vcpus=8, memory_gb=4))
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())


def test_bad_window_is_config_error_no_negative_reserve(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res, window=-3)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "ledger") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_replayed_idempotency_key_returns_original_no_double_charge(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            first = await _admit(conn, resource=res, idempotency_key="k1")
            assert first.granted is True
            assert first.allocation is not None
            replay = await _admit(conn, resource=res, idempotency_key="k1")
            assert replay.granted is True
            assert replay.allocation is not None
            assert replay.allocation.id == first.allocation.id
            # no second grant, ledger row, or spent bump.
            assert await _count(conn, "allocations") == 1
            assert await _count(conn, "ledger") == 1
            assert await _spent(conn) == Decimal("6.0000")

    asyncio.run(_run())


def test_resolve_replay_returns_none_without_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            replay = await allocation_idempotency.resolve_replay(
                conn,
                principal=CTX.principal,
                key="missing",
                kind="allocations.request",
                operation_label="allocation request",
            )

        assert replay is None

    asyncio.run(_run())


def test_resolve_replay_returns_original_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            first = await _admit(conn, resource=res, idempotency_key="replay")
            assert first.allocation is not None

            replay = await allocation_idempotency.resolve_replay(
                conn,
                principal=CTX.principal,
                key="replay",
                kind="allocations.request",
                operation_label="allocation request",
            )

        assert replay is not None
        assert replay.id == first.allocation.id

    asyncio.run(_run())


def test_resolve_replay_missing_allocation_reference_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            missing_id = uuid4()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        "stale",
                        CTX.principal,
                        "proj",
                        "allocations.request",
                        Jsonb({"allocation_id": str(missing_id)}),
                    ),
                )
            with pytest.raises(RuntimeError, match=str(missing_id)):
                await allocation_idempotency.resolve_replay(
                    conn,
                    principal=CTX.principal,
                    key="stale",
                    kind="allocations.request",
                    operation_label="allocation request",
                )

    asyncio.run(_run())


def test_record_key_duplicate_maps_to_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            allocation_id = uuid4()
            await allocation_idempotency.record_key(
                conn,
                principal=CTX.principal,
                key="dup",
                project="proj",
                kind="allocations.request",
                allocation_id=allocation_id,
            )
            with pytest.raises(CategorizedError) as caught:
                await allocation_idempotency.record_key(
                    conn,
                    principal=CTX.principal,
                    key="dup",
                    project="proj",
                    kind="allocations.request",
                    allocation_id=allocation_id,
                )

        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details == {"principal": CTX.principal}

    asyncio.run(_run())


def test_same_key_reused_across_projects_is_config_error(migrated_url: str) -> None:
    # A key that already names a grant in another project cannot resolve here — returning
    # the foreign allocation would be a cross-project replay. Fail closed.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            await BUDGETS.upsert(
                conn,
                Budget(
                    project="proj2", limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT
                ),
            )
            await QUOTAS.upsert(
                conn,
                Quota(
                    project="proj2",
                    max_concurrent_allocations=10,
                    max_concurrent_systems=10,
                    updated_at=_DT,
                ),
            )
            ctx = RequestContext(principal="alice", agent_session="s", projects=("proj", "proj2"))
            first = await admit(
                conn,
                AllocationRequest(
                    ctx=ctx,
                    resource=res,
                    project="proj",
                    selector=SEL,
                    window=2,
                    idempotency_key="dup",
                ),
            )
            assert first.granted is True
            clash = await admit(
                conn,
                AllocationRequest(
                    ctx=ctx,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="dup",
                ),
            )
            assert clash.granted is False
            assert clash.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 1  # no grant for proj2

    asyncio.run(_run())


def test_same_key_two_principals_are_isolated(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            await QUOTAS.upsert(
                conn,
                Quota(
                    project="proj2",
                    max_concurrent_allocations=10,
                    max_concurrent_systems=10,
                    updated_at=_DT,
                ),
            )
            await BUDGETS.upsert(
                conn,
                Budget(
                    project="proj2", limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT
                ),
            )
            alice = RequestContext(principal="alice", agent_session="s", projects=("proj2",))
            bob = RequestContext(principal="bob", agent_session="s", projects=("proj2",))
            a = await admit(
                conn,
                AllocationRequest(
                    ctx=alice,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="shared",
                ),
            )
            b = await admit(
                conn,
                AllocationRequest(
                    ctx=bob,
                    resource=res,
                    project="proj2",
                    selector=SEL,
                    window=2,
                    idempotency_key="shared",
                ),
            )
            assert a.granted and b.granted
            assert a.allocation is not None and b.allocation is not None
            # Same key, different principals → two distinct grants (not a replay).
            assert a.allocation.id != b.allocation.id
            assert await _count(conn, "allocations") == 2

    asyncio.run(_run())


def test_request_does_not_replay_a_renew_key(migrated_url: str) -> None:
    # The mirror of test_key_reused_across_request_kind_is_rejected (renew side): a
    # (principal, key) already stored under the *renew* kind must not resolve as a request
    # replay. Returning the renew's allocation as a "grant" would be a cross-kind replay
    # (the same key cannot mean a grant and a renew). admit must fail closed, with no second
    # grant, ledger row, or spend.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            # An existing allocation a prior renew (under key "dup") targeted.
            original = await _admit(conn, resource=res, idempotency_key="orig")
            assert original.granted is True
            assert original.allocation is not None
            allocs_before = await _count(conn, "allocations")
            ledger_before = await _count(conn, "ledger")
            spent_before = await _spent(conn)
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO idempotency_keys (key, principal, project, kind, result) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        "dup",
                        CTX.principal,
                        "proj",
                        "allocations.renew",
                        Jsonb({"allocation_id": str(original.allocation.id)}),
                    ),
                )
            clash = await _admit(conn, resource=res, idempotency_key="dup")
            assert clash.granted is False
            assert clash.category is ErrorCategory.CONFIGURATION_ERROR
            assert clash.allocation is None
            assert await _count(conn, "allocations") == allocs_before
            assert await _count(conn, "ledger") == ledger_before
            assert await _spent(conn) == spent_before

    asyncio.run(_run())


def test_host_cap_denies_allocation_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, cap=1)
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn, allocs=10)
            first = await _admit(conn, resource=res)
            assert first.granted is True
            second = await _admit(conn, resource=res)
            assert second.granted is False
            assert second.category is ErrorCategory.ALLOCATION_DENIED
            assert second.reason == "at_capacity"

    asyncio.run(_run())


def test_bad_host_cap_fails_closed_no_row(migrated_url: str) -> None:
    # The budget/quota checks pass; the M0 host-cap resolve then fails closed on an
    # invalid cap — no allocation/ledger/audit row, and no reserve debit, must survive.
    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn)
            res.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] = "not-an-int"
            await _seed_budget(conn, limit="100")
            await _seed_quota(conn)
            outcome = await _admit(conn, resource=res)
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0
            assert await _spent(conn) == Decimal(0)

    asyncio.run(_run())


def test_estimate_too_large_fails_closed_no_row(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An extreme clamp-max × a max-size selector overflows the kcu quantizer; admit must
    # return a typed configuration_error denial, never let the exception escape.
    monkeypatch.setenv("KDIVE_LEASE_MAX", "1e30")

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            res = await _seed_resource(conn, vcpus=2_000_000_000, memory_mb=64_000_000_000)
            await _seed_budget(conn, limit="1e40")
            await _seed_quota(conn)
            outcome = await _admit(
                conn,
                resource=res,
                selector=Selector(vcpus=2_000_000_000, memory_gb=0),
                window="1e30",
            )
            assert outcome.granted is False
            assert outcome.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _count(conn, "allocations") == 0
            assert await _count(conn, "ledger") == 0

    asyncio.run(_run())
