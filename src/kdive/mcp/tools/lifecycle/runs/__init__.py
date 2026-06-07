"""Registrar and compatibility exports for the `runs.*` MCP tools."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.lifecycle.runs.build import build_run, complete_build
from kdive.mcp.tools.lifecycle.runs.create import create_run
from kdive.mcp.tools.lifecycle.runs.steps import boot_run, install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run

__all__ = [
    "boot_run",
    "build_run",
    "complete_build",
    "create_run",
    "get_run",
    "install_run",
    "register",
]


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Render a Run; a failed Run maps to a failure envelope. Requires viewer."""
        return await get_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        system_id: Annotated[str, Field(description="Ready System (active Allocation) to bind.")],
        build_profile: Annotated[
            dict[str, Any], Field(description="Build profile for the Run's kernel.")
        ],
    ) -> ToolResponse:
        """Bind a Run to a ready System and Investigation in one transaction. Requires operator."""
        return await create_run(
            pool,
            current_context(),
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
        )

    @app.tool(
        name="runs.build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_build(
        run_id: Annotated[str, Field(description="The Run to build.")],
        cmdline: Annotated[
            str | None,
            Field(
                description="Kernel debug args appended to the platform-required boot args "
                "(e.g. 'dhash_entries=1'). Omit for no extra debug args. Bound on the first "
                "build of a Run."
            ),
        ] = None,
    ) -> ToolResponse:
        """Enqueue the kernel build job for a Run; poll jobs.* for completion. Requires operator."""
        return await build_run(pool, current_context(), run_id, cmdline=cmdline)

    @app.tool(
        name="runs.complete_build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_complete_build(
        run_id: Annotated[str, Field(description="The external-build Run to finalize.")],
        cmdline: Annotated[
            str,
            Field(
                description="Kernel debug args appended to the platform-required boot args "
                "(e.g. 'dhash_entries=1'). Recorded in the build ledger and applied at boot "
                "via runs.install/runs.boot (ADR-0061)."
            ),
        ],
        build_id: Annotated[
            str | None,
            Field(
                description="GNU build-id as hex (e.g. from `readelf -n vmlinux`); required iff "
                "a vmlinux was uploaded. Case-insensitive."
            ),
        ] = None,
    ) -> ToolResponse:
        """Validate an external Run's uploads and finalize it to succeeded. Operator only."""
        return await complete_build(
            pool, current_context(), run_id, build_id=build_id, cmdline=cmdline
        )

    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
    ) -> ToolResponse:
        """Enqueue the install job for a built Run; poll jobs.* for completion. Operator only."""
        return await install_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
    ) -> ToolResponse:
        """Enqueue the boot job for an installed Run; poll jobs.* for completion. Operator only."""
        return await boot_run(pool, current_context(), run_id)
