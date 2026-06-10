"""``secrets.list`` — platform-operator-gated secret *presence* (#252, ADR-0089 §6).

The handler is called directly with an injected pool + SecretRegistry + RequestContext
(the repo's unit contract). Coverage:

* a non-platform principal is DENIED (``authorization_denied``) and, because it holds no
  platform role, is NOT audited (no write amplification on a recon primitive);
* ``platform_admin`` ALONE is denied (admin implies only auditor) and the over-reach is
  audited;
* a ``platform_operator`` gets the registered *references* (scope keys) — never the secret
  values — and the served read is audited with the operator's actor/role/scope;
* the empty registry returns an empty presence set.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops import secrets
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.security.secrets.secret_registry import SecretRegistry

_SECRET_VALUE = "s3cr3t-value"  # pragma: allowlist secret - test fixture, not a real secret
_REF = "ref://build/key"


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx(
    *,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    roles: dict[str, Role] | None = None,
    projects: tuple[str, ...] = (),
    client_id: str | None = None,
) -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session="sess-1",
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
        client_id=client_id,
    )


_OPERATOR = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}), client_id="kdivectl")


def _registry_with_secret() -> SecretRegistry:
    registry = SecretRegistry()
    registry.register(_SECRET_VALUE, scope=_REF)
    return registry


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope, actor FROM platform_audit_log"
        )
        return list(await cur.fetchall())


def test_denies_non_platform_principal_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await secrets.list_secrets_tool(pool, _registry_with_secret(), ctx)
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            assert _SECRET_VALUE not in str(resp.model_dump())
        assert await _platform_audit_rows(migrated_url) == []  # no write amplification

    asyncio.run(_run())


def test_platform_admin_alone_denied_but_audited(migrated_url: str) -> None:
    # platform_admin implies only platform_auditor (NOT operator): admin alone is DENIED,
    # and because it holds a platform role the over-reach IS audited.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await secrets.list_secrets_tool(pool, _registry_with_secret(), ctx)
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"
            assert _SECRET_VALUE not in str(resp.model_dump())
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"
        assert rows[0][2] == "secrets.list"

    asyncio.run(_run())


def test_platform_operator_gets_presence_only_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await secrets.list_secrets_tool(pool, _registry_with_secret(), _OPERATOR)
            assert resp.status == "ok"
            assert _REF in resp.data["secrets"]  # the reference (scope key) — presence
            assert _SECRET_VALUE not in str(resp.model_dump())  # never the value
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0] == (
            "op-1",
            "platform_operator",
            "secrets.list",
            "all-projects",
            "operator-cli",
        )

    asyncio.run(_run())


def test_empty_registry_returns_empty_presence(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await secrets.list_secrets_tool(pool, SecretRegistry(), _OPERATOR)
            assert resp.status == "ok"
            assert resp.data["secrets"] == []

    asyncio.run(_run())
