"""allocations.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, BUDGETS, QUOTAS
from kdive.domain.errors import ErrorCategory
from kdive.domain.models import Allocation, Budget, Quota
from kdive.domain.state import AllocationState, IllegalTransition
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools.lifecycle import allocations as alloc_tools
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.resources.discovery import register_discovered_resource
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _register(
    pool: AsyncConnectionPool, *, cap: int = 1, limit: str = "1000000", quota: int = 1_000_000
) -> str:
    disc = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn, disc.list_resources()[0], pool="local-libvirt", cost_class="local"
        )
        # The M1 gate denies a project with no budget/quota row; seed generous rows so the
        # host cap (or the explicit test budget) is the binding constraint.
        await BUDGETS.upsert(
            conn,
            Budget(project="proj", limit_kcu=Decimal(limit), spent_kcu=Decimal(0), updated_at=_DT),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=quota,
                max_concurrent_systems=quota,
                updated_at=_DT,
            ),
        )
    return str(res.id)


async def _request(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str = "proj",
    vcpus: int | None = 1,
    memory_gb: int | None = 0,
    disk_gb: int | None = 10,
    shape: str | None = None,
    window: object | None = None,
    idempotency_key: str | None = None,
) -> ToolResponse:
    request: dict[str, object] = {
        "window": window,
        "resource": {"mode": "kind", "kind": "local-libvirt"},
    }
    if shape is not None:
        request["shape"] = shape
    else:
        request["vcpus"] = vcpus
        request["memory_gb"] = memory_gb
        request["disk_gb"] = disk_gb
    return await alloc_tools.request_allocation(
        pool,
        ctx,
        project=project,
        request=AllocationRequestPayload.model_validate(request),
        idempotency_key=idempotency_key,
    )


async def _request_by_id(
    pool: AsyncConnectionPool, ctx: RequestContext, resource_id: str, *, project: str = "proj"
) -> ToolResponse:
    return await alloc_tools.request_allocation(
        pool,
        ctx,
        project=project,
        request=AllocationRequestPayload.model_validate(
            {
                "vcpus": 1,
                "memory_gb": 0,
                "disk_gb": 10,
                "window": None,
                "resource": {"mode": "id", "resource_id": resource_id},
            }
        ),
    )


async def _set_resource_flags(
    pool: AsyncConnectionPool,
    resource_id: str,
    *,
    cordoned: bool | None = None,
    status: str | None = None,
) -> None:
    async with pool.connection() as conn:
        if cordoned is not None:
            await conn.execute(
                "UPDATE resources SET cordoned = %s WHERE id = %s", (cordoned, UUID(resource_id))
            )
        if status is not None:
            await conn.execute(
                "UPDATE resources SET status = %s WHERE id = %s", (status, UUID(resource_id))
            )


async def _seed_alloc(pool: AsyncConnectionPool, resource_id: str, state: AllocationState) -> str:
    # A queued `requested` row holds no host: resource_id must be NULL (the 0016 CHECK).
    placed = state is not AllocationState.REQUESTED
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                resource_id=UUID(resource_id) if placed else None,
                state=state,
            ),
        )
    return str(alloc.id)


def test_request_under_cap_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            resp = await _request(pool, _ctx())
        assert resp.status == "granted"
        assert resp.error_category is None
        assert resp.data["project"] == "proj"

    asyncio.run(_run())


def test_request_at_cap_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=1)
            await _request(pool, _ctx())
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "allocation_denied"
        assert resp.object_id == res_id
        assert resp.data["reason"] == "at_capacity"

    asyncio.run(_run())


def test_request_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            try:
                await _request(pool, _ctx(Role.VIEWER))
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_request_no_resource_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_own_allocation_returns_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            resp = await alloc_tools.get_allocation(pool, _ctx(), req.object_id)
        assert resp.object_id == req.object_id
        assert resp.status == "granted"

    asyncio.run(_run())


def test_get_allocation_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            with pytest.raises(AuthorizationError):
                await alloc_tools.get_allocation(pool, _ctx(role=None), req.object_id)

    asyncio.run(_run())


def test_get_other_project_allocation_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            other = _ctx(projects=("elsewhere",), role=Role.OPERATOR)
            resp = await alloc_tools.get_allocation(pool, other, req.object_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_failed_allocation_renders_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.FAILED)
            resp = await alloc_tools.get_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "infrastructure_failure"
        assert resp.data["current_status"] == "failed"

    asyncio.run(_run())


def test_release_granted_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=2)
            req = await _request(pool, _ctx())
            resp = await alloc_tools.release_allocation(pool, _ctx(), req.object_id)
            assert resp.status == "released"
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (req.object_id,)
                )
                row = await cur.fetchone()
            # ->granted (admission) + granted->releasing + releasing->released
            assert row is not None and row[0] == 3

    asyncio.run(_run())


def test_release_active_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.ACTIVE)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "released"

    asyncio.run(_run())


def test_release_terminal_allocation_is_stale_handle(migrated_url: str) -> None:
    # A terminal allocation was already reconciled (by a prior release or the ->expired
    # sweep); re-releasing it is a stale handle, not a config error (ADR-0040 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.RELEASED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "stale_handle"
        assert resp.data["current_status"] == "released"

    asyncio.run(_run())


def test_release_requested_allocation_cancels_with_no_credit(migrated_url: str) -> None:
    # A queued `requested` row was never reserved (ADR-0069): release cancels it directly to
    # `released` — no `releasing` hop, no ledger credit, no active_ended_at stamp.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.REQUESTED)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
            assert resp.status == "released"
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT state, active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM ledger WHERE allocation_id = %s", (alloc_id,)
                )
                ledger = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM audit_log WHERE object_id = %s", (alloc_id,)
                )
                audit = await cur.fetchone()
            assert row is not None and row[0] == "released" and row[1] is None
            assert ledger is not None and ledger[0] == 0  # never reserved → no credit
            # Exactly one audit row: requested->released (no releasing hop).
            assert audit is not None and audit[0] == 1

    asyncio.run(_run())


def test_release_illegal_transition_backstop_returns_failure(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the backstop: a state change slips past the locked re-read (a future
    # provision path could do this). update_state raising IllegalTransition must map to a
    # clean configuration_error envelope carrying the actual current state, not a 500.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            alloc_id = await _seed_alloc(pool, res_id, AllocationState.GRANTED)

            async def _boom(*args: object, **kwargs: object) -> object:
                raise IllegalTransition("forced")

            monkeypatch.setattr(ALLOCATIONS, "update_state", _boom)
            resp = await alloc_tools.release_allocation(pool, _ctx(), alloc_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "granted"  # re-read on a fresh connection

    asyncio.run(_run())


def test_release_response_includes_service_error_details() -> None:
    uid = uuid4()
    outcome = alloc_tools.ReleaseOutcome(
        released=False,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": "state"},
    )

    resp = alloc_tools._release_response(uid, outcome)

    assert resp.data["field"] == "state"


def test_renew_response_includes_service_error_details() -> None:
    uid = uuid4()
    outcome = alloc_tools.RenewOutcome(
        renewed=False,
        allocation=None,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"window": "0"},
    )

    resp = alloc_tools._renew_response(uid, outcome)

    assert resp.data["window"] == "0"


def test_list_returns_project_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=3)
            await _request(pool, _ctx())
            await _request(pool, _ctx())
            responses = await alloc_tools.list_allocations(pool, _ctx(), project="proj", limit=50)
        items = responses.items
        assert responses.object_id == "allocations"
        assert responses.status == "ok"
        assert responses.data["project"] == "proj"
        assert len(items) == 2
        assert all(r.status == "granted" for r in items)

    asyncio.run(_run())


def test_list_allocations_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=1)
            await _request(pool, _ctx())
            with pytest.raises(AuthorizationError):
                await alloc_tools.list_allocations(pool, _ctx(role=None), project="proj", limit=50)

    asyncio.run(_run())


def test_pick_by_kind_skips_cordoned_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, cordoned=True)
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_pick_by_kind_skips_non_available_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, status="degraded")
            resp = await _request(pool, _ctx())
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_cordoned_host_is_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, cordoned=True)
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_non_available_host_is_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            await _set_resource_flags(pool, res_id, status="offline")
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_explicit_id_naming_schedulable_host_grants(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            resp = await _request_by_id(pool, _ctx(), res_id)
        assert resp.status == "granted"
        assert resp.data["resource_id"] == res_id

    asyncio.run(_run())


def test_existing_allocations_untouched_when_host_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=2)
            granted = await _request(pool, _ctx())
            await _set_resource_flags(pool, res_id, cordoned=True)
            # The existing allocation is still readable and unchanged; cordon only gates
            # new placement, never live allocations.
            existing = await alloc_tools.get_allocation(pool, _ctx(), granted.object_id)
        assert existing.object_id == granted.object_id
        assert existing.status == "granted"

    asyncio.run(_run())


def test_uncordon_restores_both_placement_paths(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool, cap=4)
            await _set_resource_flags(pool, res_id, cordoned=True)
            assert (await _request(pool, _ctx())).status == "error"
            assert (await _request_by_id(pool, _ctx(), res_id)).status == "error"
            await _set_resource_flags(pool, res_id, cordoned=False)
            by_kind = await _request(pool, _ctx())
            by_id = await _request_by_id(pool, _ctx(), res_id)
        assert by_kind.status == "granted"
        assert by_id.status == "granted"

    asyncio.run(_run())


# --- M1.4 shape selector (#161) -------------------------------------------------------

# The seed shapes (migration 0013): name -> (vcpus, memory_mb, disk_gb).
_SEED_SHAPES = {
    "small": (1, 1024, 10),
    "medium": (2, 4096, 20),
    "large": (4, 8192, 40),
    "max": (8, 16384, 80),
}


async def _fetch_alloc(pool: AsyncConnectionPool, alloc_id: str) -> Allocation:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, UUID(alloc_id))
    assert alloc is not None
    return alloc


def test_shape_request_persists_resolved_tuple_and_shape_label(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="medium", window=1)
            assert resp.status == "granted"
            alloc = await _fetch_alloc(pool, resp.object_id)
        # medium = 2 vcpu / 4096 MB / 20 GB; memory_mb -> memory_gb is lossless.
        assert alloc.requested_vcpus == 2
        assert alloc.requested_memory_gb == 4
        assert alloc.requested_disk_gb == 20
        assert alloc.shape == "medium"

    asyncio.run(_run())


def test_custom_request_records_null_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), vcpus=2, memory_gb=4, disk_gb=20, window=1)
            assert resp.status == "granted"
            alloc = await _fetch_alloc(pool, resp.object_id)
        assert alloc.shape is None
        assert alloc.requested_disk_gb == 20

    asyncio.run(_run())


def test_unknown_shape_fails_closed_with_no_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="gpu-xl", window=1)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                count = await conn.execute("SELECT count(*) FROM allocations")
                row = await count.fetchone()
        assert row is not None and row[0] == 0

    asyncio.run(_run())


def test_over_host_shape_fails_closed_with_no_durable_write(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            # Add a shape larger than the fake host (8 vcpu / 16384 MB).
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) "
                    "VALUES ('huge', 64, 131072, 500)"
                )
            resp = await _request(pool, _ctx(), shape="huge", window=1)
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                for table in ("allocations", "ledger", "audit_log"):
                    row = await (await conn.execute(f"SELECT count(*) FROM {table}")).fetchone()
                    assert row is not None and row[0] == 0, table

    asyncio.run(_run())


def test_shapes_set_after_stamping_does_not_resize_allocation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool, cap=4)
            resp = await _request(pool, _ctx(), shape="medium", window=1)
            assert resp.status == "granted"
            # Redefine `medium` in the catalog AFTER the allocation is stamped.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE system_shapes SET vcpus = 8, memory_mb = 16384, disk_gb = 80 "
                    "WHERE name = 'medium'"
                )
            alloc = await _fetch_alloc(pool, resp.object_id)
        # The stamped snapshot is unchanged — the catalog edit is not retroactive.
        assert alloc.requested_vcpus == 2
        assert alloc.requested_memory_gb == 4
        assert alloc.requested_disk_gb == 20
        assert alloc.shape == "medium"

    asyncio.run(_run())
