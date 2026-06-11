"""Server-process readiness probe builders (ADR-0090 §5).

Binds the generic, dependency-set-agnostic :mod:`kdive.health` primitives to the
server's concrete backends: a Postgres ``SELECT 1`` over the shared pool and an OIDC
discovery/JWKS reachability ``HEAD``. Kept out of :mod:`kdive.health` so that package
stays free of server-stack imports (the worker/reconciler reuse it with their own
probes in issue #267).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import OIDC_JWKS_URI
from kdive.domain.errors import CategorizedError, ErrorCategory

#: Reachability-probe timeout (seconds) for the OIDC JWKS HEAD; bounded so a hung IdP
#: reads as down within the per-check timeout rather than stalling the probe.
_OIDC_PROBE_TIMEOUT = 1.5


def build_postgres_ping(pool: AsyncConnectionPool) -> Callable[[], Awaitable[None]]:
    """Return a coroutine running ``SELECT 1`` over the shared pool (raises if down)."""

    async def ping() -> None:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()

    return ping


def build_oidc_ping() -> Callable[[], Awaitable[None]]:
    """Return a coroutine probing OIDC JWKS reachability with a bounded ``GET``.

    Raises (inside the returned coroutine):
        CategorizedError: ``KDIVE_OIDC_JWKS_URI`` is unset
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`); surfaced as a failed check.
    """
    jwks_uri = config.get(OIDC_JWKS_URI)
    if not jwks_uri:
        raise CategorizedError(
            f"{OIDC_JWKS_URI.name} is not set; cannot probe OIDC readiness",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"variable": OIDC_JWKS_URI.name, "suggest": OIDC_JWKS_URI.suggest},
        )

    async def ping() -> None:
        async with httpx.AsyncClient(timeout=_OIDC_PROBE_TIMEOUT) as client:
            response = await client.get(jwks_uri)
            response.raise_for_status()

    return ping
