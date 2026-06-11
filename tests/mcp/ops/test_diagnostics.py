"""``ops.diagnostics`` — the authz-gated aggregating diagnostics tool (ADR-0091 §4).

The handler is called directly with an injected pool + service factory + RequestContext
(the repo's unit contract). Coverage maps to the #269 acceptance bullets:

* the tool is reachable only behind the ``platform_operator`` gate — a project-only token
  is denied and (holding no platform role) NOT audited; a ``platform_admin``-alone token is
  denied and the over-reach IS audited; an operator is served and the run is audited under
  the resolved ``(principal, operator-cli)`` actor;
* a down dependency surfaces as an ``error`` result (with a blocked-reason detail), not a
  contract ``fail`` — the aggregate verdict reports the error distinctly;
* the verdict carries each check's three-state status / detail / fix / provider, and the
  ``secret_ref`` aggregate never carries a per-tenant ref identifier.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage
from kdive.diagnostics.service import DiagnosticsService
from kdive.mcp.tools.ops import diagnostics
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role

_CLI_CLIENT_ID = "kdivectl"


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


class _FakeCheck(Check):
    def __init__(self, result: CheckResult) -> None:
        self._result = result

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        return self._result


def _factory(results: list[CheckResult]) -> diagnostics.ServiceFactory:
    def _build(provider: str | None, *, with_egress: bool = False) -> DiagnosticsService:
        return DiagnosticsService(checks=[_FakeCheck(r) for r in results], per_check_timeout=1.0)

    return _build


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope, actor FROM platform_audit_log"
        )
        return list(await cur.fetchall())


async def _count_platform_audit(url: str) -> int:
    return len(await _platform_audit_rows(url))


_HEALTHY = [
    CheckResult(check_id="secret_ref", status=CheckStatus.PASS, detail="all 2 resolve"),
    CheckResult(
        check_id="provider_tls",
        status=CheckStatus.PASS,
        detail="validates",
        provider="remote-libvirt",
    ),
]


# ---- authorization ------------------------------------------------------------------


def test_project_only_token_denied_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(roles={"proj-a": Role.ADMIN}, projects=("proj-a",))
            resp = await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.suggested_next_actions == ["ops.diagnostics"]
        assert await _count_platform_audit(migrated_url) == 0

    asyncio.run(_run())


def test_admin_alone_denied_but_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}))
            resp = await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), ctx)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_admin"

    asyncio.run(_run())


def test_operator_served_and_audited_under_operator_cli(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(
                platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
                client_id=_CLI_CLIENT_ID,
            )
            resp = await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), ctx)
        assert resp.status == "ok"
        assert resp.error_category is None
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"
        assert rows[0][2] == "ops.diagnostics"
        assert rows[0][4] == "operator-cli"

    asyncio.run(_run())


# ---- opt-in egress: distinct audit + factory threading ------------------------------


def test_read_only_run_records_only_the_run_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), ctx)
        rows = await _platform_audit_rows(migrated_url)
        tools = [r[2] for r in rows]
        # The default (no --with-egress) run audits once, under the read-only run tool only.
        assert tools == ["ops.diagnostics"]

    asyncio.run(_run())


def test_with_egress_records_a_distinct_provisioning_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), ctx, with_egress=True)
        rows = await _platform_audit_rows(migrated_url)
        tools = sorted(r[2] for r in rows)
        # The mutating opt-in is audited distinctly from the read-only run (ADR-0091 §4).
        assert tools == ["ops.diagnostics", "ops.diagnostics.egress"]

    asyncio.run(_run())


def test_with_egress_threads_the_opt_in_into_the_factory(migrated_url: str) -> None:
    seen: list[bool] = []

    def _factory_recording(
        provider: str | None, *, with_egress: bool = False
    ) -> DiagnosticsService:
        seen.append(with_egress)
        return DiagnosticsService(checks=[_FakeCheck(r) for r in _HEALTHY], per_check_timeout=1.0)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            await diagnostics.run_diagnostics(pool, _factory_recording, ctx, with_egress=True)
        assert seen == [True]

    asyncio.run(_run())


# ---- verdict shape ------------------------------------------------------------------


def test_verdict_carries_each_check_status_detail_fix_provider(migrated_url: str) -> None:
    results = [
        CheckResult(
            check_id="gdbstub_acl",
            status=CheckStatus.FAIL,
            detail="range blocked",
            fix="open the host firewall / ACL for it",
            provider="remote-libvirt",
        ),
    ]

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await diagnostics.run_diagnostics(pool, _factory(results), ctx)
        assert resp.status == "ok"
        item = resp.items[0]
        assert item.data["check"] == "gdbstub_acl"
        assert item.data["status"] == "fail"
        assert item.data["fix"] == "open the host firewall / ACL for it"
        assert item.data["provider"] == "remote-libvirt"
        assert resp.data["has_failure"] == "true"

    asyncio.run(_run())


def test_down_dependency_is_error_not_failure(migrated_url: str) -> None:
    results = [
        CheckResult(
            check_id="provider_tls",
            status=CheckStatus.ERROR,
            detail="provider host unreachable; cannot validate the TLS chain",
            provider="remote-libvirt",
        ),
    ]

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await diagnostics.run_diagnostics(pool, _factory(results), ctx)
        assert resp.status == "ok"
        item = resp.items[0]
        assert item.data["status"] == "error"
        assert item.data["fix"] is None
        assert "unreachable" in item.data["detail"]
        # An error is reported distinctly and never inflated into a contract failure.
        assert resp.data["has_error"] == "true"
        assert resp.data["has_failure"] == "false"

    asyncio.run(_run())


def test_secret_ref_aggregate_carries_no_per_tenant_ref(migrated_url: str) -> None:
    results = [
        CheckResult(
            check_id="secret_ref",
            status=CheckStatus.FAIL,
            detail="1 of 2 configured secret refs do not resolve",
            fix="create the file-ref or fix the path",
        ),
    ]

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await diagnostics.run_diagnostics(pool, _factory(results), ctx)
        item = resp.items[0]
        assert "project/" not in item.data["detail"]
        assert item.data["status"] == "fail"

    asyncio.run(_run())


def test_factory_build_failure_is_error_verdict_and_audited(migrated_url: str) -> None:
    def _failing_factory(provider: str | None, *, with_egress: bool = False) -> DiagnosticsService:
        raise RuntimeError("malformed KDIVE_* secret value")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = _ctx(platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}))
            resp = await diagnostics.run_diagnostics(pool, _failing_factory, ctx)
        # A build/config fault is a distinct error verdict, not an unhandled crash or a fail.
        assert resp.status == "ok"
        item = resp.items[0]
        assert item.data["status"] == "error"
        assert item.data["fix"] is None
        assert resp.data["has_error"] == "true"
        assert resp.data["has_failure"] == "false"
        # The served attempt is still audited.
        rows = await _platform_audit_rows(migrated_url)
        assert len(rows) == 1
        assert rows[0][1] == "platform_operator"

    asyncio.run(_run())
