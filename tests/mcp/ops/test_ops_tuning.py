"""Runtime-tuning ops tools (#139) — coeff upsert + host-capacity, gating, and audit.

The handlers are called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #139 acceptance bullets:

* set_cost_class_coeff: the next ``resolve_coeff`` returns the new value (upsert of an
  existing class and insert of a new class); committed ledger rows are unchanged; fail-closed
  on a non-positive / non-numeric coeff; platform_operator gating (denied + audit-iff-role).
* set_host_capacity: admission honors the new cap; lowering below the live count blocks new
  placement WITHOUT evicting the live allocations; unknown id rejected; negative cap rejected;
  platform_operator gating.
* success rows land in ``platform_audit_log`` with the tuned target as scope.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.cost import Selector, resolve_coeff
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops import tuning
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.allocation.admission import AllocationRequest, admit

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    *,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    roles: dict[str, Role] | None = None,
    projects: tuple[str, ...] = (),
    principal: str = "op-1",
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
    )


_OPERATOR = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _resource(
    conn: psycopg.AsyncConnection, *, cap: int = 5, cost_class: str = "local"
) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={
                CONCURRENT_ALLOCATION_CAP_KEY: cap,
                "vcpus": 16,
                "memory_mb": 65536,
            },
            pool="local-libvirt",
            cost_class=cost_class,
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return res.id


async def _alloc(
    conn: psycopg.AsyncConnection,
    resource_id: UUID,
    project: str,
    state: AllocationState = AllocationState.ACTIVE,
) -> UUID:
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            agent_session="sess",
            project=project,
            resource_id=resource_id,
            state=state,
            lease_expiry=None,
            capability_scope={},
        ),
    )
    return alloc.id


async def _budget_quota(conn: psycopg.AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 100000, 0)",
            (project,),
        )
        await cur.execute(
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, 100, 100)",
            (project,),
        )


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    return len(await _platform_audit_rows(url))


async def _live_count(conn: psycopg.AsyncConnection, resource_id: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM allocations WHERE resource_id = %s AND state <> 'released'",
            (resource_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# ---- set_cost_class_coeff -------------------------------------------------------------


def test_set_coeff_changes_next_charge_resolution(migrated_url: str) -> None:
    # After set_cost_class_coeff, the next resolve_coeff (the pricing read) returns the new
    # value — committed ledger rows are not touched (none exist; the upsert is pricing-only).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")
            resp = await tuning.set_cost_class_coeff(
                pool, _OPERATOR, cost_class="local", coeff="2.5"
            )
            assert resp.status == "ok"
            assert resp.data["coeff"] == "2.5"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("2.5")
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0] == ("op-1", "platform_operator", "ops.set_cost_class_coeff", "local")

    asyncio.run(_run())


def test_set_coeff_inserts_new_class(migrated_url: str) -> None:
    # A class with no row is fail-closed today (resolve_coeff raises); the upsert seeds it.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="gpu", coeff="7")
            assert resp.status == "ok"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "gpu") == Decimal("7")

    asyncio.run(_run())


def test_set_coeff_does_not_reprice_committed_ledger(migrated_url: str) -> None:
    # Committed ledger rows are priced at write time; a later coeff change leaves them as-is.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                await _budget_quota(conn, "proj-a")
            request = AllocationRequest(
                ctx=_ctx(roles={"proj-a": Role.OPERATOR}, projects=("proj-a",), principal="alice"),
                resource=await _get_resource(pool, res),
                project="proj-a",
                selector=Selector(vcpus=2, memory_gb=4, cost_class="local"),
                window=1,
            )
            async with pool.connection() as conn:
                outcome = await admit(conn, request)
            assert outcome.granted
            before = await _ledger_rows(migrated_url)
            await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class="local", coeff="9")
            after = await _ledger_rows(migrated_url)
            assert before == after  # the reserved row's kcu is unchanged by the reprice

    asyncio.run(_run())


def test_set_coeff_rejects_non_positive(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("0", "-1", "nan", "infinity", "abc"):
                resp = await tuning.set_cost_class_coeff(
                    pool, _OPERATOR, cost_class="local", coeff=bad
                )
                assert resp.status == "error", bad
                assert resp.error_category == "configuration_error", bad
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")  # untouched

    asyncio.run(_run())


def test_set_coeff_rejects_blank_cost_class(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("", "   "):
                resp = await tuning.set_cost_class_coeff(pool, _OPERATOR, cost_class=bad, coeff="2")
                assert resp.status == "error", repr(bad)
                assert resp.error_category == "configuration_error", repr(bad)
        assert await _count_platform_audit(migrated_url) == 0  # nothing applied, nothing audited

    asyncio.run(_run())


# ---- set_host_capacity ----------------------------------------------------------------


def test_set_capacity_updates_capabilities_jsonb(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=2
            )
            assert resp.status == "ok"
            assert resp.data[CONCURRENT_ALLOCATION_CAP_KEY] == "2"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 2
            # the other capabilities are preserved by the jsonb merge
            assert row.capabilities["vcpus"] == 16
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "ops.set_host_capacity", str(res))]

    asyncio.run(_run())


def test_set_capacity_blocks_new_placement_without_evicting(migrated_url: str) -> None:
    # Two live allocations occupy a host with cap 5. Lower the cap to 2 (below the live
    # count): the two live allocations stay (no eviction), but a new admission is denied.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
                await _budget_quota(conn, "proj-a")
                a1 = await _alloc(conn, res, "proj-a")
                a2 = await _alloc(conn, res, "proj-a")
            await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=2
            )
            # no eviction: both live allocations still occupy the host
            async with pool.connection() as conn:
                assert await _live_count(conn, res) == 2
                row_a1 = await ALLOCATIONS.get(conn, a1)
                row_a2 = await ALLOCATIONS.get(conn, a2)
            assert row_a1 is not None and row_a1.state is AllocationState.ACTIVE
            assert row_a2 is not None and row_a2.state is AllocationState.ACTIVE
            # admission honors the new cap: live(2) >= cap(2) → denied, no durable write
            request = AllocationRequest(
                ctx=_ctx(roles={"proj-a": Role.OPERATOR}, projects=("proj-a",), principal="bob"),
                resource=await _get_resource(pool, res),
                project="proj-a",
                selector=Selector(vcpus=1, memory_gb=1, cost_class="local"),
                window=1,
            )
            async with pool.connection() as conn:
                outcome = await admit(conn, request)
            assert not outcome.granted
            assert outcome.reason == "at_capacity"
            assert outcome.cap == 2
            assert outcome.in_use == 2

    asyncio.run(_run())


def test_set_capacity_unknown_resource_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(uuid4()), concurrent_allocation_cap=3
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_capacity_rejects_negative_cap(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id=str(res), concurrent_allocation_cap=-1
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5  # untouched

    asyncio.run(_run())


def test_set_capacity_malformed_id_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await tuning.set_host_capacity(
                pool, _OPERATOR, resource_id="not-a-uuid", concurrent_allocation_cap=3
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"

    asyncio.run(_run())


# ---- platform_operator gating ---------------------------------------------------------


def test_coeff_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await tuning.set_cost_class_coeff(pool, ctx, cost_class="local", coeff="3")
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await resolve_coeff(conn, "local") == Decimal("1.0")  # not applied
        assert await _count_platform_audit(migrated_url) == 0  # no write amplification

    asyncio.run(_run())


def test_capacity_denied_for_auditor_but_audited(migrated_url: str) -> None:
    # platform_auditor does NOT satisfy the operator gate, but holds a platform role, so the
    # over-reach denial IS audited (the accountability target).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn, cap=5)
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await tuning.set_host_capacity(
                pool, ctx, resource_id=str(res), concurrent_allocation_cap=1
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                row = await RESOURCES.get(conn, res)
            assert row is not None
            assert row.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5  # not applied
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_auditor"
        assert rows[0][2] == "ops.set_host_capacity"

    asyncio.run(_run())


def test_admin_does_not_imply_operator_gate(migrated_url: str) -> None:
    # platform_admin implies only platform_auditor (not operator), so admin is DENIED here —
    # the operator/admin separation of duties holds.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await tuning.set_cost_class_coeff(pool, ctx, cost_class="local", coeff="3")
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"

    asyncio.run(_run())


# ---- helpers needing the pool ---------------------------------------------------------


async def _get_resource(pool: AsyncConnectionPool, resource_id: UUID) -> Resource:
    async with pool.connection() as conn:
        row = await RESOURCES.get(conn, resource_id)
    assert row is not None
    return row


async def _ledger_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT allocation_id, cost_class, event_type, kcu_delta FROM ledger ORDER BY id"
        )
        return list(await cur.fetchall())
