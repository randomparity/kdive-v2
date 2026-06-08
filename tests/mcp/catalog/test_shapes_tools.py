"""Shapes catalog tools (#160) — list (viewer), set/delete (platform_operator).

The handlers are called directly with an injected pool + RequestContext (the repo's unit
contract). Coverage maps to the #160 acceptance bullets:

* shapes.list: returns the migration seed, sorted, for any authenticated context (the
  catalog is project-less, so the "viewer on any project" gate collapses to authenticated —
  the resources.list precedent).
* shapes.set: upserts (insert a new shape AND update an existing one); validates whole-GB
  memory_mb and pcie_match grammar (configuration_error, not applied); stores a valid
  pcie_match; rejects a blank name / non-positive dims; platform_operator gating (denied +
  audit-iff-role).
* shapes.delete: removes the row; unknown name is configuration_error and writes no audit;
  a delete with live allocations/systems present leaves those rows unchanged (there is no
  shape FK by construction today, so this asserts non-interference, not a label reference).
* every successful mutation lands one platform_audit_log row with the shape name as scope.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEM_SHAPES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import shapes
from kdive.security.authz.rbac import PlatformRole, Role

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_SEED_NAMES = ["large", "max", "medium", "small"]  # sorted by name


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
_VIEWER = _ctx(roles={"proj-a": Role.VIEWER}, projects=("proj-a",), principal="viewer-1")


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT principal, platform_role, tool, scope FROM platform_audit_log")
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    return len(await _platform_audit_rows(url))


async def _resource(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={"vcpus": 16, "memory_mb": 65536},
            pool="local-libvirt",
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return res.id


async def _alloc(conn: psycopg.AsyncConnection, resource_id: UUID, project: str) -> UUID:
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
            state=AllocationState.ACTIVE,
            lease_expiry=None,
            capability_scope={},
        ),
    )
    return alloc.id


# ---- shapes.list ----------------------------------------------------------------------


def test_list_returns_seed_sorted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.list_shapes(pool, _VIEWER)
        assert resp.status == "ok"
        assert resp.data["count"] == "4"
        assert [item.object_id for item in resp.items] == _SEED_NAMES
        small = next(item for item in resp.items if item.object_id == "small")
        assert small.data["vcpus"] == "1"
        assert small.data["memory_mb"] == "1024"
        assert small.data["disk_gb"] == "10"

    asyncio.run(_run())


def test_list_reflects_a_set(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await shapes.set_shape(
                pool, _OPERATOR, name="gpu-xl", vcpus=16, memory_mb=32768, disk_gb=200
            )
            resp = await shapes.list_shapes(pool, _VIEWER)
        assert "gpu-xl" in [item.object_id for item in resp.items]

    asyncio.run(_run())


# ---- shapes.set -----------------------------------------------------------------------


def test_set_inserts_new_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool, _OPERATOR, name="gpu", vcpus=8, memory_mb=8192, disk_gb=100
            )
            assert resp.status == "ok"
            assert resp.data["name"] == "gpu"
            async with pool.connection() as conn:
                row = await SYSTEM_SHAPES.get(conn, "gpu")
            assert row is not None
            assert row.vcpus == 8
            assert row.memory_mb == 8192
            assert row.disk_gb == 100
            assert row.pcie_match is None
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "shapes.set", "gpu")]

    asyncio.run(_run())


def test_set_upserts_existing_shape(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool, _OPERATOR, name="small", vcpus=2, memory_mb=2048, disk_gb=15
            )
            assert resp.status == "ok"
            async with pool.connection() as conn:
                row = await SYSTEM_SHAPES.get(conn, "small")
            assert row is not None
            assert row.vcpus == 2
            assert row.memory_mb == 2048
            assert row.disk_gb == 15
            # still exactly four seeded names (an update, not a new row)
            async with pool.connection() as conn:
                all_shapes = await SYSTEM_SHAPES.list_all(conn)
            assert sorted(s.name for s in all_shapes) == _SEED_NAMES

    asyncio.run(_run())


def test_set_stores_valid_pcie_match(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool,
                _OPERATOR,
                name="nic",
                vcpus=2,
                memory_mb=4096,
                disk_gb=20,
                pcie_match="8086:1572",
            )
            assert resp.status == "ok"
            assert resp.data["pcie_match"] == "8086:1572"
            async with pool.connection() as conn:
                row = await SYSTEM_SHAPES.get(conn, "nic")
            assert row is not None
            assert row.pcie_match == "8086:1572"

    asyncio.run(_run())


def test_set_rejects_non_whole_gb_memory(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool, _OPERATOR, name="odd", vcpus=2, memory_mb=4097, disk_gb=20
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "odd") is None  # not applied
        assert await _count_platform_audit(migrated_url) == 0  # nothing applied, nothing audited

    asyncio.run(_run())


def test_set_rejects_malformed_pcie_match(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("not-a-spec", "8086:157", "class=zz", "8086:1572 "):
                resp = await shapes.set_shape(
                    pool, _OPERATOR, name="nic", vcpus=2, memory_mb=4096, disk_gb=20, pcie_match=bad
                )
                assert resp.status == "error", bad
                assert resp.error_category == "configuration_error", bad
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "nic") is None  # not applied
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_normalizes_padded_name(migrated_url: str) -> None:
    # A padded name must be stored stripped, so the resolver/list see the canonical key (no
    # unresolvable shadow row) and the audit scope matches.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool, _OPERATOR, name="  gpu  ", vcpus=8, memory_mb=8192, disk_gb=100
            )
            assert resp.status == "ok"
            assert resp.object_id == "gpu"
            assert resp.data["name"] == "gpu"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "gpu") is not None
                assert await SYSTEM_SHAPES.get(conn, "  gpu  ") is None
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "shapes.set", "gpu")]

    asyncio.run(_run())


def test_set_rejects_over_long_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.set_shape(
                pool, _OPERATOR, name="x" * 65, vcpus=2, memory_mb=4096, disk_gb=20
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_rejects_blank_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for bad in ("", "   "):
                resp = await shapes.set_shape(
                    pool, _OPERATOR, name=bad, vcpus=2, memory_mb=4096, disk_gb=20
                )
                assert resp.status == "error", repr(bad)
                assert resp.error_category == "configuration_error", repr(bad)
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_rejects_non_positive_dims(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            cases = (
                (0, 4096, 20),
                (2, 0, 20),
                (2, 4096, 0),
                (-1, 4096, 20),
            )
            for vcpus, memory_mb, disk_gb in cases:
                resp = await shapes.set_shape(
                    pool, _OPERATOR, name="bad", vcpus=vcpus, memory_mb=memory_mb, disk_gb=disk_gb
                )
                assert resp.status == "error", (vcpus, memory_mb, disk_gb)
                assert resp.error_category == "configuration_error", (vcpus, memory_mb, disk_gb)
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "bad") is None
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_set_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await shapes.set_shape(
                pool, ctx, name="gpu", vcpus=8, memory_mb=8192, disk_gb=100
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "gpu") is None  # not applied
        assert await _count_platform_audit(migrated_url) == 0  # no write amplification

    asyncio.run(_run())


def test_set_denied_for_auditor_but_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}))
            resp = await shapes.set_shape(
                pool, ctx, name="gpu", vcpus=8, memory_mb=8192, disk_gb=100
            )
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "gpu") is None  # not applied
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_auditor"
        assert rows[0][2] == "shapes.set"

    asyncio.run(_run())


# ---- shapes.delete --------------------------------------------------------------------


def test_delete_removes_shape_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.delete_shape(pool, _OPERATOR, name="small")
            assert resp.status == "deleted"
            assert resp.object_id == "small"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "small") is None
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "shapes.delete", "small")]

    asyncio.run(_run())


def test_delete_normalizes_padded_name(migrated_url: str) -> None:
    # A padded delete resolves the same canonical key set stores (symmetry with set).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.delete_shape(pool, _OPERATOR, name="  medium  ")
            assert resp.status == "deleted"
            assert resp.object_id == "medium"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "medium") is None
        rows = await _platform_audit_rows(migrated_url)
        assert rows == [("op-1", "platform_operator", "shapes.delete", "medium")]

    asyncio.run(_run())


def test_delete_unknown_is_configuration_error_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await shapes.delete_shape(pool, _OPERATOR, name="nope")
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
        assert await _count_platform_audit(migrated_url) == 0  # nothing removed, nothing audited

    asyncio.run(_run())


def test_delete_denied_for_project_only_token_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await shapes.delete_shape(pool, ctx, name="small")
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await SYSTEM_SHAPES.get(conn, "small") is not None  # not removed
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_delete_does_not_disturb_live_rows(migrated_url: str) -> None:
    # The `shape` name on allocations/systems is a label, not an FK (and that column does not
    # exist yet — it lands in a later M1.4 issue), so deleting a shape never FK-blocks and
    # never re-sizes a live row. Assert non-interference: live rows are unchanged.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                res = await _resource(conn)
                alloc_id = await _alloc(conn, res, "proj-a")
            before = await _allocation_snapshot(pool, alloc_id)
            resp = await shapes.delete_shape(pool, _OPERATOR, name="medium")
            assert resp.status == "deleted"
            after = await _allocation_snapshot(pool, alloc_id)
        assert before == after

    asyncio.run(_run())


async def _allocation_snapshot(pool: AsyncConnectionPool, alloc_id: UUID) -> tuple[object, ...]:
    async with pool.connection() as conn:
        row = await ALLOCATIONS.get(conn, alloc_id)
    assert row is not None
    return (row.state, row.resource_id, row.project, row.principal)
