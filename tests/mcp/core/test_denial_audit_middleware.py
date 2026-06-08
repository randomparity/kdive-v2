"""Dispatch-boundary denial-audit middleware (ADR-0062 §5, issue #142).

The middleware catches a member-over-reach `RoleDenied` at the one MCP tool-dispatch
boundary, writes a single `audit_log` denial row (object NULL, `transition='denied'`,
`project` taken from the exception), and returns an authorization-denied envelope.
Non-membership denials (base
`AuthorizationError`), `require_platform_role` denials, and `DestructiveOpDenied` are
NOT caught here — confirming no double-write / no write-amplification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.middleware import DenialAuditMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.gate import DestructiveOpDenied
from kdive.security.rbac import AuthorizationError, Role, RoleDenied


class _FakeMessage:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContext:
    def __init__(self, tool: str) -> None:
        self.message = _FakeMessage(tool)


def _role_denied() -> RoleDenied:
    return RoleDenied(
        principal="alice",
        project="proj",
        held=Role.VIEWER,
        required=Role.OPERATOR,
    )


def _count_audit(conn: psycopg.AsyncConnection) -> Awaitable[int]:
    async def _go() -> int:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM audit_log")
            row = await cur.fetchone()
        assert row is not None
        return int(row[0])

    return _go()


def _drive(
    migrated_url: str,
    raised: BaseException,
    *,
    expect_type: type[BaseException],
    agent_session: str | None = "sess-1",
) -> list[tuple[Any, ...]]:
    """Run the middleware over a call_next that raises ``raised``; return audit rows."""

    async def _run() -> list[tuple[Any, ...]]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = DenialAuditMiddleware(pool, agent_session=lambda: agent_session)

            async def _call_next(_ctx: Any) -> None:
                raise raised

            with pytest.raises(expect_type) as excinfo:
                await mw.on_call_tool(_FakeContext("allocations.release"), _call_next)
            # The original exception is re-raised unchanged (identity preserved).
            assert excinfo.value is raised
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, object_kind, "
                    "object_id, transition, reason FROM audit_log ORDER BY ts"
                )
                return await cur.fetchall()

    return asyncio.run(_run())


def _drive_role_denied(
    migrated_url: str,
    denial: RoleDenied,
    *,
    agent_session: str | None = "sess-1",
) -> tuple[ToolResponse, list[tuple[Any, ...]]]:
    async def _run() -> tuple[ToolResponse, list[tuple[Any, ...]]]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = DenialAuditMiddleware(pool, agent_session=lambda: agent_session)

            async def _call_next(_ctx: Any) -> None:
                raise denial

            result = await mw.on_call_tool(_FakeContext("allocations.release"), _call_next)
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, object_kind, "
                    "object_id, transition, reason FROM audit_log ORDER BY ts"
                )
                return result, await cur.fetchall()

    return asyncio.run(_run())


def test_role_denied_writes_one_denial_row_and_returns_envelope(migrated_url: str) -> None:
    resp, rows = _drive_role_denied(migrated_url, _role_denied())
    assert resp.object_id == "allocations.release"
    assert resp.error_category == "authorization_denied"
    assert len(rows) == 1
    principal, agent_session, project, tool, kind, obj, transition, reason = rows[0]
    assert (principal, agent_session, project, tool) == (
        "alice",
        "sess-1",
        "proj",
        "allocations.release",
    )
    assert kind is None and obj is None  # object-agnostic boundary
    assert transition == "denied"
    assert "operator" in reason  # the human-readable denial reason is captured


def test_role_denied_audit_failure_still_returns_envelope(
    migrated_url: str,
) -> None:
    class _FailingRecordMiddleware(DenialAuditMiddleware):
        async def _record(self, tool: str, denial: RoleDenied) -> None:
            _ = (tool, denial)
            raise RuntimeError("audit unavailable")

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = _FailingRecordMiddleware(pool, agent_session=lambda: "sess-1")

            async def _call_next(_ctx: Any) -> None:
                raise _role_denied()

            result = await mw.on_call_tool(_FakeContext("allocations.release"), _call_next)
            assert result.error_category == "authorization_denied"
            async with pool.connection() as conn:
                assert await _count_audit(conn) == 0

    asyncio.run(_run())


def test_role_denied_project_comes_from_exception_not_call_args(migrated_url: str) -> None:
    # The middleware never inspects tool arguments for project; it reads it off RoleDenied.
    denial = RoleDenied(
        principal="bob", project="resolved-from-row", held=None, required=Role.ADMIN
    )
    _resp, rows = _drive_role_denied(migrated_url, denial)
    assert len(rows) == 1
    assert rows[0][2] == "resolved-from-row"


def test_null_agent_session_persists(migrated_url: str) -> None:
    _resp, rows = _drive_role_denied(migrated_url, _role_denied(), agent_session=None)
    assert len(rows) == 1
    assert rows[0][1] is None


def test_base_authorization_error_is_not_audited(migrated_url: str) -> None:
    # Non-membership denial: base AuthorizationError → re-raised, NO row (no amplification).
    rows = _drive(migrated_url, AuthorizationError("not a member"), expect_type=AuthorizationError)
    assert rows == []


def test_destructive_op_denied_is_not_audited(migrated_url: str) -> None:
    # DestructiveOpDenied is an AuthorizationError subclass already audited by its own
    # handler — the RoleDenied-specific catch must NOT double-write it.
    rows = _drive(
        migrated_url, DestructiveOpDenied(["capability_scope"]), expect_type=DestructiveOpDenied
    )
    assert rows == []


def test_unrelated_exception_passes_through_unaudited(migrated_url: str) -> None:
    rows = _drive(migrated_url, RuntimeError("boom"), expect_type=RuntimeError)
    assert rows == []


def test_successful_call_writes_no_denial_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = DenialAuditMiddleware(pool, agent_session=lambda: "sess-1")
            sentinel = object()

            async def _call_next(_ctx: Any) -> object:
                return sentinel

            result = await mw.on_call_tool(_FakeContext("allocations.release"), _call_next)
            assert result is sentinel
            async with pool.connection() as conn:
                assert await _count_audit(conn) == 0

    asyncio.run(_run())


_CALL_NEXT_TYPE = Callable[[Any], Awaitable[Any]]  # documents the call_next contract
