"""``build_hosts.*`` MCP tool registration (ADR-0099, issue #342).

Registers four tools on the FastMCP ``app``:

* ``build_hosts.register`` — add a new SSH build host (platform_admin, mutating).
* ``build_hosts.list``    — enumerate all hosts (read-only).
* ``build_hosts.disable`` — set enabled=false on a host (platform_admin, mutating).
* ``build_hosts.remove``  — delete a host row (platform_admin, mutating).
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops.build_hosts.manage import (
    DISABLE_TOOL,
    LIST_TOOL,
    REMOVE_TOOL,
    disable_build_host,
    list_build_hosts,
    remove_build_host,
)
from kdive.mcp.tools.ops.build_hosts.register import REGISTER_TOOL, register_build_host


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``build_hosts.*`` tools on ``app``, bound to ``pool``."""

    @app.tool(name=REGISTER_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_register(
        name: Annotated[
            str, Field(description="Unique human-readable identifier for the new host.")
        ],
        address: Annotated[str, Field(description="SSH hostname or IP address of the build host.")],
        ssh_credential_ref: Annotated[
            str,
            Field(
                description=(
                    "Credential secret reference (e.g. 'ssh://build-host-key'). "
                    "Only the reference string is stored — secret bytes are never fetched."
                )
            ),
        ],
        workspace_root: Annotated[
            str,
            Field(description="Absolute path on the build host where builds are staged."),
        ],
        max_concurrent: Annotated[
            int, Field(description="Maximum simultaneous build leases this host may hold (> 0).")
        ],
    ) -> ToolResponse:
        """Register a new SSH build host in the inventory. Requires platform_admin."""
        return await register_build_host(
            pool,
            current_context(),
            name=name,
            address=address,
            ssh_credential_ref=ssh_credential_ref,
            workspace_root=workspace_root,
            max_concurrent=max_concurrent,
        )

    @app.tool(name=LIST_TOOL, annotations=_docmeta.read_only(), meta={"maturity": "implemented"})
    async def build_hosts_list() -> ToolResponse:
        """List all registered build hosts (id, name, kind, address, credential ref, state)."""
        return await list_build_hosts(pool, current_context())

    @app.tool(name=DISABLE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_disable(
        name: Annotated[str, Field(description="The build host name to disable.")],
    ) -> ToolResponse:
        """Disable a build host so the scheduler will not select it. Requires platform_admin."""
        return await disable_build_host(pool, current_context(), name=name)

    @app.tool(name=REMOVE_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def build_hosts_remove(
        name: Annotated[str, Field(description="The build host name to remove.")],
    ) -> ToolResponse:
        """Delete a build host from the inventory. Requires platform_admin."""
        return await remove_build_host(pool, current_context(), name=name)


__all__ = ["register"]
