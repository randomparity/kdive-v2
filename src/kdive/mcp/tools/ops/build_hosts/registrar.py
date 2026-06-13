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
        workspace_root: Annotated[
            str,
            Field(description="Absolute path where builds are staged (in-guest for ephemeral)."),
        ],
        max_concurrent: Annotated[
            int, Field(description="Maximum simultaneous build leases this host may hold (> 0).")
        ],
        kind: Annotated[
            str, Field(description="Build host kind: 'ssh' (default) or 'ephemeral_libvirt'.")
        ] = "ssh",
        address: Annotated[
            str | None, Field(description="SSH hostname or IP address (ssh kind only).")
        ] = None,
        ssh_credential_ref: Annotated[
            str | None,
            Field(
                description=(
                    "Credential secret reference, e.g. 'ssh://build-host-key' (ssh kind only). "
                    "Only the reference string is stored — secret bytes are never fetched."
                )
            ),
        ] = None,
        base_image_volume: Annotated[
            str | None,
            Field(description="Base build-image volume name (ephemeral_libvirt kind only)."),
        ] = None,
    ) -> ToolResponse:
        """Register a new remote build host (ssh or ephemeral_libvirt). Requires platform_admin."""
        return await register_build_host(
            pool,
            current_context(),
            name=name,
            workspace_root=workspace_root,
            max_concurrent=max_concurrent,
            kind=kind,
            address=address,
            ssh_credential_ref=ssh_credential_ref,
            base_image_volume=base_image_volume,
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
