"""accounting.usage_project handler tests — viewer-scoped spend rollup (ADR-0007 §6).

usage has two call forms: by project (require_project + require_role(viewer)) and by
investigation_id (resolve the owning project, then the identical check on it — no
cross-project read bypass). It reports the O(1) spent/remaining totals plus the ledger
by_cost_class and shared_kcu breakdown. Handlers are called directly with an injected pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import (
    ALLOCATIONS,
    BUDGETS,
    INVESTIGATIONS,
    RESOURCES,
    RUNS,
    SYSTEMS,
)
from kdive.domain.models import (
    Allocation,
    Budget,
    Investigation,
    Resource,
    ResourceKind,
    Run,
    System,
)
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    ResourceStatus,
    RunState,
    SystemState,
)
from kdive.mcp.auth import AuthError, RequestContext
from kdive.mcp.tools.accounting.usage import usage_investigation, usage_project
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.services.accounting import ledger as accounting
from tests.mcp.roles import PROJECT_A, PROJECT_B, make_role_fixture

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {p: role for p in projects} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_spend(pool: AsyncConnectionPool, project: str = "proj") -> None:
    async with pool.connection() as conn:
        await BUDGETS.upsert(
            conn,
            Budget(project=project, limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT),
        )
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
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=res.id,
                state=AllocationState.ACTIVE,
                requested_vcpus=2,
                requested_memory_gb=4,
                active_started_at=_DT,
                active_ended_at=_DT + timedelta(hours=2),
            ),
        )
        await accounting.reserve(conn, alloc, Decimal("9.0000"))
        await accounting.reconcile(conn, alloc)


async def _seed_investigation(pool: AsyncConnectionPool, project: str) -> UUID:
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                title="inv",
                state=InvestigationState.OPEN,
            ),
        )
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
        await BUDGETS.upsert(
            conn,
            Budget(project=project, limit_kcu=Decimal("100"), spent_kcu=Decimal(0), updated_at=_DT),
        )
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=res.id,
                state=AllocationState.ACTIVE,
                requested_vcpus=2,
                requested_memory_gb=4,
            ),
        )
        await accounting.reserve(conn, alloc, Decimal("3.0000"))
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile={},
            ),
        )
        await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=inv.id,
                system_id=system.id,
                state=RunState.CREATED,
                build_profile={},
            ),
        )
    return inv.id


def test_usage_by_project_reports_totals(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_spend(pool)
            resp = await usage_project(pool, _ctx(), project="proj")
        assert resp.status == "ok"
        assert resp.error_category is None
        assert resp.suggested_next_actions == ["accounting.estimate", "allocations.list"]
        assert resp.data["project"] == "proj"
        assert resp.data["spent_kcu"] == "6.0000"
        assert resp.data["budget_remaining"] == "94.0000"
        assert resp.data["shared_kcu"] == "0.0000"
        assert resp.data["by_cost_class"] == {"local": "6.0000"}

    asyncio.run(_run())


def test_usage_by_project_requires_viewer(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            try:
                await usage_project(pool, _ctx(role=None), project="proj")
                raise AssertionError("expected AuthorizationError")
            except AuthorizationError:
                pass

    asyncio.run(_run())


def test_usage_foreign_project_refused(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            other = _ctx(projects=("elsewhere",))
            try:
                await usage_project(pool, other, project="proj")
                raise AssertionError("expected AuthError")
            except AuthError:
                pass

    asyncio.run(_run())


def test_usage_by_investigation_resolves_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, "proj")
            resp = await usage_investigation(pool, _ctx(), investigation_id=str(inv_id))
        assert resp.status == "ok"
        assert resp.data["project"] == "proj"
        assert resp.data["investigation_id"] == str(inv_id)
        assert resp.data["investigation_kcu"] == "3.0000"

    asyncio.run(_run())


def test_usage_by_investigation_unknown_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await usage_investigation(pool, _ctx(), investigation_id=str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.usage_investigation"]

    asyncio.run(_run())


def test_usage_by_investigation_malformed_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await usage_investigation(pool, _ctx(), investigation_id="not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.suggested_next_actions == ["accounting.usage_investigation"]

    asyncio.run(_run())


def test_viewer_in_a_refused_usage_for_b_investigation(migrated_url: str) -> None:
    # The tenant-isolation boundary: a viewer in project A cannot read B's spend by
    # passing a B-owned investigation_id. The owning-project resolve + require_project
    # on it raises AuthError (ADR-0007 §6 / ADR-0037).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_b = await _seed_investigation(pool, PROJECT_B)
            viewer_a = _ctx(projects=(PROJECT_A,))
            try:
                await usage_investigation(pool, viewer_a, investigation_id=str(inv_b))
                raise AssertionError("expected AuthError")
            except AuthError:
                pass

    asyncio.run(_run())


def test_separated_fixture_viewer_reads_own_but_not_foreign(migrated_url: str) -> None:
    # Exercises the importable separated-role fixture (tests/mcp/roles.py) as the
    # cross-module artifact #68 ships: project-A's viewer reads A's own usage, and is
    # refused B's spend via a B-owned investigation_id (the acceptance pairing).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_spend(pool, PROJECT_A)
            inv_b = await _seed_investigation(pool, PROJECT_B)
            fx = make_role_fixture()
            viewer_a = fx.project(PROJECT_A).viewer.ctx

            own = await usage_project(pool, viewer_a, project=PROJECT_A)
            assert own.status == "ok"
            assert own.data["project"] == PROJECT_A

            try:
                await usage_investigation(pool, viewer_a, investigation_id=str(inv_b))
                raise AssertionError("expected AuthError for a foreign investigation_id")
            except AuthError:
                pass

    asyncio.run(_run())
