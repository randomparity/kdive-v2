"""Registrar and compatibility exports for the `systems.*` MCP tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.lifecycle.systems.admin import reprovision_system, teardown_system
from kdive.mcp.tools.lifecycle.systems.provision import (
    define_system,
    get_system,
    provision_defined_system,
    provision_system,
)
from kdive.providers.composition import ProviderRuntime, build_default_provider_runtime

__all__ = [
    "define_system",
    "get_system",
    "provision_defined_system",
    "provision_system",
    "register",
    "reprovision_system",
    "teardown_system",
]


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, provider_runtime: ProviderRuntime | None = None
) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""
    runtime = provider_runtime or build_default_provider_runtime()

    @app.tool(
        name="systems.define",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_define(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to create a DEFINED System for.")
        ],
        profile: Annotated[
            dict[str, Any],
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation (upload window). Operator only."""
        return await define_system(
            pool,
            current_context(),
            allocation_id=allocation_id,
            profile=profile,
            component_sources=runtime.component_sources,
            rootfs_validator=runtime.rootfs_validator,
        )

    @app.tool(
        name="systems.provision",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            dict[str, Any],
            Field(description="Provisioning profile for the System create lane."),
        ],
    ) -> ToolResponse:
        """Mint a System for a granted Allocation and enqueue provision. Operator only."""
        return await provision_system(
            pool,
            current_context(),
            allocation_id=allocation_id,
            profile=profile,
            component_sources=runtime.component_sources,
            rootfs_validator=runtime.rootfs_validator,
        )

    @app.tool(
        name="systems.provision_defined",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_provision_defined(
        system_id: Annotated[
            str,
            Field(description="Defined System whose stored profile should be provisioned."),
        ],
    ) -> ToolResponse:
        """Admit a DEFINED System after its upload window is complete. Requires operator."""
        return await provision_defined_system(
            pool,
            current_context(),
            system_id=system_id,
            component_sources=runtime.component_sources,
            rootfs_validator=runtime.rootfs_validator,
        )

    @app.tool(
        name="systems.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_get(
        system_id: Annotated[str, Field(description="The System to render.")],
    ) -> ToolResponse:
        """Render a System; failed maps to a failure envelope. Requires viewer."""
        return await get_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
    ) -> ToolResponse:
        """Enqueue an idempotent teardown for a System; destroys the domain. Requires admin."""
        return await teardown_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            dict[str, Any],
            Field(description="New provisioning profile; must opt in to reprovision."),
        ],
    ) -> ToolResponse:
        """Reprovision a ready System in place under its Allocation. Requires operator + gate."""
        return await reprovision_system(
            pool,
            current_context(),
            system_id=system_id,
            profile=profile,
            component_sources=runtime.component_sources,
            rootfs_validator=runtime.rootfs_validator,
        )
