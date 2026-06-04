"""investigations.* tool tests — handlers called directly with an injected pool + ctx."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.state import InvestigationState
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools import investigations as inv_tools
from kdive.security.rbac import AuthorizationError, Role


def _ctx(
    role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="user-1", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _open(pool: AsyncConnectionPool, ctx: RequestContext, **kw: Any):
    return await inv_tools.open_investigation(pool, ctx, **kw)


def test_open_mints_investigation_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _open(pool, _ctx(), project="proj", title="kernel oops in xfs")
            assert resp.status == "open"
            inv_id = resp.object_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, title FROM investigations WHERE id = %s", (inv_id,)
                )
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = '->open' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "open" and row["title"] == "kernel oops in xfs"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_open_persists_and_dedups_external_refs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            refs = [
                {"tracker": "bz", "id": "42", "url": "https://bz/42"},
                {"tracker": "bz", "id": "42", "url": "https://bz/42-dup"},  # same (tracker,id)
                {"tracker": "jira", "id": "K-1", "url": "https://jira/K-1"},
            ]
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=refs)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT external_refs FROM investigations WHERE id = %s", (resp.object_id,)
                )
                row = await cur.fetchone()
        assert row is not None
        stored = {(r["tracker"], r["id"]): r["url"] for r in row["external_refs"]}
        assert stored == {("bz", "42"): "https://bz/42-dup", ("jira", "K-1"): "https://jira/K-1"}

    asyncio.run(_run())


def test_open_malformed_external_ref_is_config_error_no_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            bad = [{"tracker": "bz", "id": "42"}]  # missing url
            resp = await _open(pool, _ctx(), project="proj", title="t", external_refs=bad)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM investigations")
                n = await cur.fetchone()
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert n is not None and n["n"] == 0

    asyncio.run(_run())


def test_open_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            with pytest.raises(AuthorizationError):
                await _open(pool, _ctx(Role.VIEWER), project="proj", title="t")

    asyncio.run(_run())


def test_get_own_investigation_renders_state(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(pool, _ctx(), opened.object_id)
        assert resp.status == "open"
        assert resp.data["external_refs"] == "0"

    asyncio.run(_run())


def test_get_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            opened = await _open(pool, _ctx(), project="proj", title="t")
            resp = await inv_tools.get_investigation(
                pool, _ctx(projects=("other",)), opened.object_id
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_uuid_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await inv_tools.get_investigation(pool, _ctx(), "not-a-uuid")
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


async def _seed_investigation(pool: AsyncConnectionPool, state: InvestigationState) -> str:
    """Insert an Investigation directly in ``state`` (bypassing the open->… tools)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.models import Investigation

    dt = datetime(2026, 1, 1, tzinfo=UTC)
    async with pool.connection() as conn:
        inv = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=dt,
                updated_at=dt,
                principal="user-1",
                project="proj",
                title="seeded",
                state=state,
            ),
        )
    return str(inv.id)


def test_close_open_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM investigations WHERE id = %s", (inv_id,))
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE transition = 'open->closed' "
                    "AND object_id = %s",
                    (inv_id,),
                )
                audit = await cur.fetchone()
        assert row is not None and row["state"] == "closed"
        assert audit is not None and audit["n"] == 1

    asyncio.run(_run())


def test_close_active_investigation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ACTIVE)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "closed"

    asyncio.run(_run())


def test_close_already_closed_is_idempotent_no_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.CLOSED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
            assert resp.status == "closed"
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log WHERE object_id = %s", (inv_id,)
                )
                audit = await cur.fetchone()
        assert audit is not None and audit["n"] == 0  # no transition audited

    asyncio.run(_run())


def test_close_abandoned_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.ABANDONED)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"
        assert resp.data["current_status"] == "abandoned"

    asyncio.run(_run())


def test_close_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            with pytest.raises(AuthorizationError):
                await inv_tools.close_investigation(pool, _ctx(Role.VIEWER), inv_id)

    asyncio.run(_run())


def test_close_cross_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            resp = await inv_tools.close_investigation(pool, _ctx(projects=("other",)), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_close_backstop_maps_illegal_transition(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    # Force the IllegalTransition backstop: make update_state raise so the handler's
    # except-branch maps it to configuration_error rather than letting it escape.
    from kdive.db.repositories import INVESTIGATIONS
    from kdive.domain.state import IllegalTransition

    async def _boom(*_a: object, **_k: object) -> object:
        raise IllegalTransition("forced")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            inv_id = await _seed_investigation(pool, InvestigationState.OPEN)
            monkeypatch.setattr(INVESTIGATIONS, "update_state", _boom)
            resp = await inv_tools.close_investigation(pool, _ctx(), inv_id)
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())
