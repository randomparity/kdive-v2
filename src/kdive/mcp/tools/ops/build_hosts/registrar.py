"""``build_hosts.*`` MCP tool registration (ADR-0099, issue #342).

Registers five tools on the FastMCP ``app``:

* ``build_hosts.register_ssh`` — add a new SSH build host (platform_admin, mutating).
* ``build_hosts.register_ephemeral_libvirt`` — add a new ephemeral-libvirt build host.
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
from kdive.mcp.tools.ops.build_hosts.lifecycle import (
    DISABLE_TOOL,
    LIST_TOOL,
    REMOVE_TOOL,
    disable_build_host,
    list_build_hosts,
    remove_build_host,
)
from kdive.mcp.tools.ops.build_hosts.register import (
    REGISTER_EPHEMERAL_LIBVIRT_TOOL,
    REGISTER_SSH_TOOL,
    register_ephemeral_libvirt_build_host,
    register_ssh_build_host,
)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``build_hosts.*`` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name=REGISTER_SSH_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def build_hosts_register_ssh(
        name: Annotated[
            str, Field(description="Unique human-readable identifier for the new host.")
        ],
        address: Annotated[str, Field(description="SSH hostname or IP address.")],
        ssh_credential_ref: Annotated[
            str,
            Field(
                description=(
                    "Credential secret reference, e.g. 'ssh://build-host-key'. "
                    "Only the reference string is stored — secret bytes are never fetched."
                )
            ),
        ],
        workspace_root: Annotated[
            str, Field(description="Absolute path where builds are staged on the SSH host.")
        ],
        max_concurrent: Annotated[
            int, Field(description="Maximum simultaneous build leases this host may hold (> 0).")
        ],
    ) -> ToolResponse:
        """Register a new SSH build host. Requires platform_admin."""
        return await register_ssh_build_host(
            pool,
            current_context(),
            name=name,
            workspace_root=workspace_root,
            max_concurrent=max_concurrent,
            address=address,
            ssh_credential_ref=ssh_credential_ref,
        )

    @app.tool(
        name=REGISTER_EPHEMERAL_LIBVIRT_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def build_hosts_register_ephemeral_libvirt(
        name: Annotated[
            str, Field(description="Unique human-readable identifier for the new host.")
        ],
        base_image_volume: Annotated[
            str, Field(description="Base build-image volume name in the remote storage pool.")
        ],
        workspace_root: Annotated[
            str, Field(description="Absolute path where builds are staged inside the build VM.")
        ],
        max_concurrent: Annotated[
            int, Field(description="Maximum simultaneous build leases this host may hold (> 0).")
        ],
    ) -> ToolResponse:
        """Register a new ephemeral-libvirt build host. Requires platform_admin."""
        return await register_ephemeral_libvirt_build_host(
            pool,
            current_context(),
            name=name,
            workspace_root=workspace_root,
            max_concurrent=max_concurrent,
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
