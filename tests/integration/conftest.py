"""Fixtures + shared helpers for the walking-skeleton integration tests (#26, ADR-0035).

Re-exports the disposable-Postgres fixtures (ADR-0015) so the non-gated exit-criterion
tests run against a freshly-migrated schema, and provides the `_pool` connection-pool
context manager and the `request_context` builder every test reuses — the same shapes the
per-plane MCP suites use, kept here so the integration module imports one place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.security.rbac import Role

# Re-export the disposable-Postgres fixtures so the integration tests can request them.
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.store.conftest import minio_store  # noqa: F401


@asynccontextmanager
async def open_pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """Yield an open async pool for ``url``, closed on exit."""
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def request_context(
    role: Role | None = Role.OPERATOR,
    *,
    principal: str = "user-1",
    projects: tuple[str, ...] = ("proj",),
) -> RequestContext:
    """Build a `RequestContext` granting ``role`` on the single test project (ADR-0035 §3)."""
    roles = {projects[0]: role} if role is not None else {}
    return RequestContext(
        principal=principal, agent_session="sess-1", projects=projects, roles=roles
    )
