"""Fixtures + shared helpers for the walking-skeleton integration tests (#26, ADR-0035).

Re-exports the disposable-Postgres fixtures (ADR-0015) so the non-gated exit-criterion
tests run against a freshly-migrated schema, and provides the `_pool` connection-pool
context manager and the `request_context` builder every test reuses — the same shapes the
per-plane MCP suites use, kept here so the integration module imports one place.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.security.authz.rbac import Role

# Re-export the disposable-Postgres fixtures so the integration tests can request them.
from tests.db.conftest import migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.store.conftest import minio_store  # noqa: F401

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_LIVE_SSH_ENV = "KDIVE_LIVE_SSH_TARGET"


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


def live_vm_preflight(*, require_ssh: bool = False) -> tuple[Path, Path]:
    """Resolve operator-provided live-VM fixtures or skip with the setup command."""
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or missing; run scripts/live-vm/build-guest-image.sh"
        )
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(
            f"{_KERNEL_TREE_ENV} unset or missing; run scripts/live-vm/fetch-kernel-tree.sh"
        )
    if require_ssh and not os.environ.get(_LIVE_SSH_ENV):
        pytest.skip(f"{_LIVE_SSH_ENV} unset; run scripts/live-vm/check-ssh-reachable.sh <host>")
    return Path(image), Path(tree)
