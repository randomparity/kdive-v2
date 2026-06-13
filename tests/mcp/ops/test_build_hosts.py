"""``build_hosts.*`` platform-admin MCP tools (ADR-0099, issue #342).

Handlers are driven directly with an injected pool + RequestContext.

Coverage:
* non-admin register/disable/remove → ``authorization_denied``
* register (admin) creates an SSH row; list shows it with the credential REF only
  (no key bytes in response); response carries id + suggested_next_actions
* register duplicate name → ``conflict``
* register with max_concurrent <= 0 → ``configuration_error``
* disable/remove of ``worker-local`` → ``conflict``
* disable/remove of absent name → ``not_found``
* remove of host with outstanding lease → ``conflict``
* audit row written for register/disable/remove; no-leak guard (no secret bytes
  in any row or error response)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools.ops.build_hosts.manage import (
    disable_build_host,
    list_build_hosts,
    remove_build_host,
)
from kdive.mcp.tools.ops.build_hosts.register import register_build_host
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

_SECRET_VALUE = "-----BEGIN OPENSSH PRIVATE KEY-----FAKE"  # pragma: allowlist secret
_CRED_REF = "ssh://build/worker-key"


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _admin_ctx(*, principal: str = "ops-admin") -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-admin",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        client_id="kdivectl",
    )


def _non_admin_ctx() -> RequestContext:
    return RequestContext(
        principal="proj-user",
        agent_session="sess-user",
        projects=("proj-a",),
        roles={},
        platform_roles=frozenset(),
        client_id=None,
    )


def _auditor_ctx() -> RequestContext:
    # platform_auditor does NOT satisfy platform_admin (the only implication is
    # admin ⊇ auditor), so it is denied — but it IS a platform role, so the denial
    # is audited (the over-reach accountability row).
    return RequestContext(
        principal="ops-auditor",
        agent_session="sess-aud",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
        client_id="kdivectl",
    )


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope, args_digest "
            "FROM platform_audit_log ORDER BY id"
        )
        return list(await cur.fetchall())


async def _host_exists(url: str, name: str) -> bool:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM build_hosts WHERE name = %s", (name,))
        return await cur.fetchone() is not None


async def _insert_host(pool: AsyncConnectionPool, *, name: str = "build-worker-1") -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO build_hosts "
            "  (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent) "
            "VALUES (%s, 'ssh', '10.0.0.1', %s, '/build', 2) RETURNING id",
            (name, _CRED_REF),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_lease(pool: AsyncConnectionPool, host_id: UUID) -> UUID:
    run_id = uuid4()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
            (run_id, host_id),
        )
    return run_id


# --- authorization gate ---


def test_non_admin_register_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _non_admin_ctx(),
                name="build-worker-1",
                address="10.0.0.1",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=2,
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert _SECRET_VALUE not in str(resp.model_dump())

    asyncio.run(_run())


def test_non_admin_disable_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_host(pool)
            resp = await disable_build_host(pool, _non_admin_ctx(), name="build-worker-1")
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())


def test_non_admin_remove_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_host(pool)
            resp = await remove_build_host(pool, _non_admin_ctx(), name="build-worker-1")
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())


def test_platform_auditor_overreach_denied_and_audited(migrated_url: str) -> None:
    # A platform_auditor holds a platform role but not platform_admin: every mutating
    # build_hosts tool denies it AND records the over-reach via audit_platform_denial.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_host(pool, name="build-worker-1")
            reg = await register_build_host(
                pool,
                _auditor_ctx(),
                name="new-host",
                address="10.0.0.9",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=1,
            )
            dis = await disable_build_host(pool, _auditor_ctx(), name="build-worker-1")
            rem = await remove_build_host(pool, _auditor_ctx(), name="build-worker-1")
        for resp in (reg, dis, rem):
            assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        rows = await _platform_audit_rows(migrated_url)
        tools = sorted(str(r[2]) for r in rows)
        assert tools == [
            "build_hosts.disable",
            "build_hosts.register",
            "build_hosts.remove",
        ]
        for principal, platform_role, _tool, scope, _digest in rows:
            assert principal == "ops-auditor"
            assert platform_role == "platform_auditor"
            assert str(scope).startswith("denied:")
        # the denied register must not have created a row
        assert await _host_exists(migrated_url, "new-host") is False

    asyncio.run(_run())


# --- register happy path + no-leak ---


def test_register_creates_ssh_row_list_shows_ref_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="build-worker-1",
                address="10.0.0.1",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=4,
            )
            assert resp.status == "registered"
            assert "id" in resp.data
            assert resp.data["name"] == "build-worker-1"
            assert "build_hosts.list" in resp.suggested_next_actions
            assert "runs.build" in resp.suggested_next_actions

            list_resp = await list_build_hosts(pool, _admin_ctx())

        assert list_resp.status == "ok"
        # Find our row in items
        names = [item.data.get("name") for item in list_resp.items]
        assert "build-worker-1" in names

        item = next(i for i in list_resp.items if i.data.get("name") == "build-worker-1")
        assert item.data["ssh_credential_ref"] == _CRED_REF
        assert item.data["kind"] == "ssh"

        # No-leak: secret value must not appear anywhere in either response
        assert _SECRET_VALUE not in str(resp.model_dump())
        assert _SECRET_VALUE not in str(list_resp.model_dump())

    asyncio.run(_run())


def test_register_audit_row_written_no_secret_bytes(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await register_build_host(
                pool,
                _admin_ctx(principal="ops-admin"),
                name="build-worker-2",
                address="10.0.0.2",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build2",
                max_concurrent=2,
            )
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        principal, platform_role, tool, scope, digest = rows[0]
        assert principal == "ops-admin"
        assert platform_role == "platform_admin"
        assert tool == "build_hosts.register"
        # scope carries the host id
        assert "build_host:" in str(scope)
        # args are digested at the DB column level: a 64-char lowercase hex SHA-256,
        # never the plaintext args (which carry the credential ref).
        assert isinstance(digest, str)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)
        assert _CRED_REF not in digest
        # secret bytes must not appear in any DB row (digest, scope, or otherwise)
        for row in rows:
            assert _SECRET_VALUE not in str(row)

    asyncio.run(_run())


# --- register conflict / config errors ---


def test_register_duplicate_name_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await register_build_host(
                pool,
                _admin_ctx(),
                name="dup-host",
                address="10.0.0.1",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=1,
            )
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="dup-host",
                address="10.0.0.2",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build2",
                max_concurrent=1,
            )
        assert resp.error_category == ErrorCategory.CONFLICT.value
        assert _SECRET_VALUE not in str(resp.model_dump())

    asyncio.run(_run())


def test_register_max_concurrent_zero_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="bad-host",
                address="10.0.0.1",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=0,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _host_exists(migrated_url, "bad-host") is False

    asyncio.run(_run())


def test_register_max_concurrent_negative_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="neg-host",
                address="10.0.0.1",
                ssh_credential_ref=_CRED_REF,
                workspace_root="/build",
                max_concurrent=-5,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


# --- register ephemeral_libvirt ---


def test_register_ephemeral_creates_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="builders",
                kind="ephemeral_libvirt",
                base_image_volume="kdive-build-base.qcow2",
                workspace_root="/build",
                max_concurrent=2,
            )
            assert resp.status == "registered"
            list_resp = await list_build_hosts(pool, _admin_ctx())
        item = next(i for i in list_resp.items if i.data.get("name") == "builders")
        assert item.data["kind"] == "ephemeral_libvirt"
        assert item.data["address"] == ""
        assert item.data["ssh_credential_ref"] == ""

    asyncio.run(_run())


def test_register_ephemeral_without_base_image_volume_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="bad-eph",
                kind="ephemeral_libvirt",
                workspace_root="/build",
                max_concurrent=2,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _host_exists(migrated_url, "bad-eph") is False

    asyncio.run(_run())


def test_register_ephemeral_with_ssh_fields_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="bad-eph2",
                kind="ephemeral_libvirt",
                address="10.0.0.9",
                base_image_volume="base.qcow2",
                workspace_root="/build",
                max_concurrent=2,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _host_exists(migrated_url, "bad-eph2") is False

    asyncio.run(_run())


def test_register_unknown_kind_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_build_host(
                pool,
                _admin_ctx(),
                name="weird",
                kind="cloud",
                workspace_root="/build",
                max_concurrent=2,
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


# --- disable: protected / not_found / audit ---


def test_disable_worker_local_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await disable_build_host(pool, _admin_ctx(), name="worker-local")
        assert resp.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


def test_disable_absent_host_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await disable_build_host(pool, _admin_ctx(), name="no-such-host")
        assert resp.error_category == ErrorCategory.NOT_FOUND.value

    asyncio.run(_run())


def test_disable_audit_row_written(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_host(pool, name="build-worker-d")
            resp = await disable_build_host(
                pool, _admin_ctx(principal="ops-admin"), name="build-worker-d"
            )
        assert resp.status == "disabled"
        rows = await _platform_audit_rows(migrated_url)
        assert any(r[2] == "build_hosts.disable" for r in rows)
        for row in rows:
            assert _SECRET_VALUE not in str(row)

    asyncio.run(_run())


# --- remove: protected / not_found / lease conflict / audit ---


def test_remove_worker_local_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await remove_build_host(pool, _admin_ctx(), name="worker-local")
        assert resp.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


def test_remove_absent_host_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await remove_build_host(pool, _admin_ctx(), name="no-such-host")
        assert resp.error_category == ErrorCategory.NOT_FOUND.value

    asyncio.run(_run())


def test_remove_host_with_active_lease_conflict(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            host_id = await _insert_host(pool, name="leased-host")
            await _insert_lease(pool, host_id)
            resp = await remove_build_host(pool, _admin_ctx(), name="leased-host")
        assert resp.error_category == ErrorCategory.CONFLICT.value
        assert await _host_exists(migrated_url, "leased-host") is True

    asyncio.run(_run())


def test_remove_audit_row_written_and_host_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_host(pool, name="build-worker-r")
            resp = await remove_build_host(
                pool, _admin_ctx(principal="ops-admin"), name="build-worker-r"
            )
        assert resp.status == "removed"
        assert await _host_exists(migrated_url, "build-worker-r") is False
        rows = await _platform_audit_rows(migrated_url)
        assert any(r[2] == "build_hosts.remove" for r in rows)
        for row in rows:
            assert _SECRET_VALUE not in str(row)

    asyncio.run(_run())
