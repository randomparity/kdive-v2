"""Tests that `record_platform` persists the audit `actor` column (ADR-0089).

The `actor` field is required on `PlatformAuditEvent` and written by the INSERT, so every
platform audit row carries a caller classification (operator-cli | agent | unknown).
"""

from __future__ import annotations

import asyncio

import psycopg

from kdive.security.audit import PlatformAuditEvent, record_platform


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_record_platform_persists_actor(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            row_id = await record_platform(
                conn,
                principal="op@example.com",
                agent_session=None,
                event=PlatformAuditEvent(
                    tool="resources.list",
                    scope="all-projects",
                    args={},
                    platform_role="platform_admin",
                    actor="operator-cli",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT actor FROM platform_audit_log WHERE id = %s", (row_id,))
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "operator-cli"

    asyncio.run(_run())


def test_record_platform_persists_unknown_actor(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            row_id = await record_platform(
                conn,
                principal="mystery",
                agent_session=None,
                event=PlatformAuditEvent(
                    tool="resources.list",
                    scope="all-projects",
                    args={},
                    platform_role="platform_auditor",
                    actor="unknown",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT actor FROM platform_audit_log WHERE id = %s", (row_id,))
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "unknown"

    asyncio.run(_run())


def test_platform_audit_event_requires_actor() -> None:
    # A required field: an unported construction site fails to construct (TypeError).
    try:
        PlatformAuditEvent(  # ty: ignore[missing-argument]
            tool="resources.list", scope="all-projects", args={}, platform_role=None
        )
    except TypeError:
        return
    raise AssertionError("PlatformAuditEvent must require an actor argument")
