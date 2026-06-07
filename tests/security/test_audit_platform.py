"""Tests for the platform read-access audit writer (ADR-0043 §4).

`record_platform` writes one `platform_audit_log` row with **no** project-membership
guard, so a principal with empty `ctx.projects` (a platform-only token) and a
`require_platform_role` denial both leave a trail the per-project `audit_log` cannot
represent.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg

from kdive.security.audit import PlatformAuditEvent, args_digest, record_platform


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _count_platform_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_record_platform_writes_row_for_empty_projects(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            audit_id = await record_platform(
                conn,
                principal="platform-bot",
                agent_session="sess-1",
                event=PlatformAuditEvent(
                    tool="accounting.report",
                    scope="all-projects",
                    args={"scope": "all-projects"},
                    platform_role="platform_auditor",
                ),
            )
            assert isinstance(audit_id, UUID)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, platform_role, tool, scope, "
                    "args_digest FROM platform_audit_log WHERE id = %s",
                    (audit_id,),
                )
                row = await cur.fetchone()
            assert row == (
                "platform-bot",
                "sess-1",
                "platform_auditor",
                "accounting.report",
                "all-projects",
                args_digest({"scope": "all-projects"}),
            )
            assert await _count_platform_audit(conn) == 1

    asyncio.run(_run_test())


def test_record_platform_persists_null_platform_role_for_member_read(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            audit_id = await record_platform(
                conn,
                principal="alice",
                agent_session=None,
                event=PlatformAuditEvent(
                    tool="accounting.report",
                    scope="granted-set:proj-a,proj-b",
                    args={},
                    platform_role=None,
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT platform_role, agent_session FROM platform_audit_log WHERE id = %s",
                    (audit_id,),
                )
                row = await cur.fetchone()
            assert row == (None, None)

    asyncio.run(_run_test())


def test_record_platform_writes_denial_row(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            # A `platform_operator` over-reaching the auditor read: the denial is
            # accountable because the principal holds a platform role.
            audit_id = await record_platform(
                conn,
                principal="operator-bot",
                agent_session="sess-9",
                event=PlatformAuditEvent(
                    tool="accounting.report",
                    scope="all-projects",
                    args={"scope": "all-projects"},
                    platform_role="platform_operator",
                ),
            )
            assert isinstance(audit_id, UUID)
            assert await _count_platform_audit(conn) == 1

    asyncio.run(_run_test())


def test_record_platform_composes_in_caller_transaction(migrated_url: str) -> None:
    class _Boom(RuntimeError):
        pass

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            try:
                async with conn.transaction():
                    await record_platform(
                        conn,
                        principal="platform-bot",
                        agent_session=None,
                        event=PlatformAuditEvent(
                            tool="accounting.report",
                            scope="all-projects",
                            args={},
                            platform_role="platform_auditor",
                        ),
                    )
                    raise _Boom
            except _Boom:
                pass
            assert await _count_platform_audit(conn) == 0  # rolled back with the caller's txn

    asyncio.run(_run_test())
