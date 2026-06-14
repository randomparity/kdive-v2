"""``ops.reconcile_systems`` handler tests (M2.6 #399, ADR-0112).

The handler is called directly with an injected pool and request context (the repo's primary
test contract). These tests prove:

* ``platform_admin`` gating: an operator (or a project-only token) is **denied** — the inventory
  pass can PRUNE, so it is gated tighter than ``ops.reconcile_now`` (platform_operator);
* the pass actually runs the inventory engine (a config ``[[fault_inject]]`` instance reconciles
  to a ``resources`` row);
* the action audits to ``platform_audit_log`` recording the actor and the resulting
  ``ReconcileDiff`` — the pruned/cordoned identities are in the human-readable ``scope`` (so a
  config-driven deletion is directly attributable) and the full diff is committed to
  ``args_digest`` (tamper-evident);
* an absent default ``systems.toml`` is a quiet no-op (no failure), still audited.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops import reconcile_systems as ops_reconcile_systems
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

_TOOL = "ops.reconcile_systems"


def _ctx(*, platform_roles: frozenset[PlatformRole] = frozenset()) -> RequestContext:
    return RequestContext(
        principal="admin-1",
        agent_session="sess-1",
        projects=(),
        roles={},
        platform_roles=platform_roles,
    )


_ADMIN = frozenset({PlatformRole.PLATFORM_ADMIN})
_OPERATOR = frozenset({PlatformRole.PLATFORM_OPERATOR})


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _audit_rows(url: str) -> list[tuple[object, ...]]:
    async with await _connect(url) as check:
        cur = await check.execute(
            "SELECT principal, platform_role, scope, args_digest FROM platform_audit_log "
            "WHERE tool = %s ORDER BY ts",
            (_TOOL,),
        )
        return await cur.fetchall()


async def _audit_count(url: str) -> int:
    return len(await _audit_rows(url))


def _write_systems_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(body)
    return path


_FAULT_INJECT_TOML = (
    "schema_version = 2\n"
    "[[fault_inject]]\n"
    'name = "fi-recon"\n'
    'cost_class = "local"\n'
    "vcpus = 8\n"
    "memory_mb = 16384\n"
)


def test_reconcile_systems_runs_inventory_and_audits_diff(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_systems_toml(tmp_path, _FAULT_INJECT_TOML)))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_ADMIN), image_store=None
            )
        assert resp.status == "ok"
        assert resp.data["created"] == "1"
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute(
                "SELECT managed_by FROM resources WHERE kind = 'fault-inject' AND name = 'fi-recon'"
            )
            row = await cur.fetchone()
        assert row is not None and row[0] == "config"  # the pass actually reconciled
        rows = await _audit_rows(migrated_url)
        assert len(rows) == 1
        principal, role, scope, digest = rows[0]
        assert principal == "admin-1"
        assert role == "platform_admin"
        assert isinstance(scope, str) and scope.startswith("all-systems")
        assert isinstance(digest, str) and digest  # the ReconcileDiff is committed to the digest

    asyncio.run(_run())


def test_reconcile_systems_audits_prunes_for_attribution(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config-driven deletion must be attributable: the pruned identity is in the audit args.
    async def _run() -> None:
        monkeypatch.setenv(
            "KDIVE_SYSTEMS_TOML", str(_write_systems_toml(tmp_path, _FAULT_INJECT_TOML))
        )
        async with _pool(migrated_url) as pool:
            await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_ADMIN), image_store=None
            )
            # Now drop the instance from the file and reconcile again -> it is pruned.
            monkeypatch.setenv(
                "KDIVE_SYSTEMS_TOML",
                str(_write_systems_toml(tmp_path, "schema_version = 2\n")),
            )
            resp = await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_ADMIN), image_store=None
            )
        assert resp.status == "ok"
        assert resp.data["pruned"] == "1"
        assert resp.data["pruned_names"] == "fi-recon"
        rows = await _audit_rows(migrated_url)
        # The second (prune) pass's audit scope names the pruned identity (attributable).
        scopes = [str(r[2]) for r in rows]
        assert any("pruned=fi-recon" in s for s in scopes)

    asyncio.run(_run())


def test_operator_is_denied_reconcile_systems(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # platform_operator is NOT enough — the inventory pass prunes, so this is platform_admin.
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_systems_toml(tmp_path, _FAULT_INJECT_TOML)))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_OPERATOR), image_store=None
            )
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == [_TOOL]
        # The denied call reconciled nothing.
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute("SELECT count(*) FROM resources WHERE name = 'fi-recon'")
            row = await cur.fetchone()
        assert row is not None and int(row[0]) == 0

    asyncio.run(_run())


def test_operator_denial_is_audited(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An overreach by a platform-role holder is audited (separation-of-duties accountability).
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_systems_toml(tmp_path, _FAULT_INJECT_TOML)))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_OPERATOR), image_store=None
            )
        assert resp.status == "error"
        rows = await _audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][0] == "admin-1"
        assert rows[0][1] == "platform_operator"

    asyncio.run(_run())


def test_project_only_non_admin_is_denied_and_writes_no_audit_row(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_systems_toml(tmp_path, _FAULT_INJECT_TOML)))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile_systems.reconcile_systems(pool, _ctx(), image_store=None)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # A project-only denial is the routine non-grant case and is NOT recorded.
        assert await _audit_count(migrated_url) == 0

    asyncio.run(_run())


def test_absent_default_systems_toml_is_quiet_no_op(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An absent default file is the normal pre-config state: the pass is a quiet no-op (no
    # failure, prunes nothing), still audited as a ran control action.
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_reconcile_systems.reconcile_systems(
                pool, _ctx(platform_roles=_ADMIN), image_store=None
            )
        assert resp.status == "ok"
        assert resp.data["created"] == "0"
        assert resp.data["pruned"] == "0"
        assert await _audit_count(migrated_url) == 1

    asyncio.run(_run())
