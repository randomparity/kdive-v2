"""Platform control-plane and break-glass MCP tools (`ops.*`, ADR-0062).

The `ops.*` namespace holds platform-operator control-plane actions (reconcile, queue
control, capacity/cost tuning) and platform-admin break-glass. Each tool gates on the M1.1
``require_platform_role`` seam and audits cross-tenant actions. Distinct from
``kdive.mcp.tools.debug.ops`` (gdb-MI debug tooling).

:func:`register` wires the break-glass tools; the queue-control tools register through their
own module (`kdive.mcp.tools.ops.queue`) in :mod:`kdive.mcp.app`.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.ops import breakglass

__all__ = ["breakglass", "register"]


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the break-glass `ops.*` tools on ``app``, bound to ``pool``."""
    breakglass.register(app, pool)
