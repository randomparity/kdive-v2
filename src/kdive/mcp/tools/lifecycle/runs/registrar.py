"""Registrar for the `runs.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.errors import CategorizedError
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.build import RunBuildHandlers as _RunBuildHandlers
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest as _RunCreateRequest,
)
from kdive.mcp.tools.lifecycle.runs.create import (
    RunReuseRequirementInput as _RunReuseRequirementInput,
)
from kdive.mcp.tools.lifecycle.runs.create import create_run as _create_run
from kdive.mcp.tools.lifecycle.runs.steps import boot_run as _boot_run
from kdive.mcp.tools.lifecycle.runs.steps import install_run as _install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.profiles.types import BuildProfileInput, ExpectedBootFailureInput
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver | None = None,
) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""
    if resolver is None:
        raise RuntimeError("runs registrar requires an injected provider resolver")

    async def _runtime_for_run(run_id: str) -> ProviderRuntime | ToolResponse:
        uid = _as_uuid(run_id)
        if uid is None:
            return _config_error(run_id)
        async with pool.connection() as conn:
            try:
                return await resolver.runtime_for_run(conn, uid)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)

    def _build_handlers(runtime: ProviderRuntime) -> _RunBuildHandlers:
        return _RunBuildHandlers(
            runtime.component_sources,
            config_validator=runtime.build_config_validator,
        )

    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Render a Run; a failed Run maps to a failure envelope. Requires viewer."""
        return await _get_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        system_id: Annotated[str, Field(description="Ready System (active Allocation) to bind.")],
        build_profile: Annotated[
            BuildProfileInput, Field(description="Build profile for the Run's kernel.")
        ],
        expected_boot_failure: Annotated[
            ExpectedBootFailureInput | None,
            Field(
                description=(
                    "Optional expected boot failure, e.g. "
                    "{'kind':'console_crash','pattern':'Oops'}."
                )
            ),
        ] = None,
        require_vcpus: Annotated[
            int | None,
            Field(
                gt=0,
                description="Optional reuse assertion: the System's persisted snapshot must have "
                "at least this many vcpus, re-checked under the lock. Omit to skip.",
            ),
        ] = None,
        require_memory_gb: Annotated[
            int | None,
            Field(
                gt=0,
                description="Optional reuse assertion: the System's snapshot must have at least "
                "this much memory in GB. Omit to skip.",
            ),
        ] = None,
        require_disk_gb: Annotated[
            int | None,
            Field(
                gt=0,
                description="Optional reuse assertion: the System's snapshot must have at least "
                "this much disk in GB. Omit to skip.",
            ),
        ] = None,
        require_pcie: Annotated[
            list[str] | None,
            Field(
                description="Optional reuse assertion: the System's allocation pcie_claim must "
                "contain each 'vendor:device' spec (e.g. ['8086:1572']). Omit or [] to skip."
            ),
        ] = None,
    ) -> ToolResponse:
        """Bind a Run to a ready System and Investigation in one transaction. Requires operator."""
        reuse_requirement = _RunReuseRequirementInput(
            vcpus=require_vcpus,
            memory_gb=require_memory_gb,
            disk_gb=require_disk_gb,
            pcie=require_pcie,
        )
        request = _RunCreateRequest(
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
            expected_boot_failure=expected_boot_failure,
            reuse_requirement=reuse_requirement,
        )
        return await _create_run(
            pool,
            current_context(),
            request,
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
        runtime = await _runtime_for_run(run_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _build_handlers(runtime).build_run(
            pool,
            current_context(),
            run_id,
            cmdline=cmdline,
        )

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
        runtime = await _runtime_for_run(run_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _build_handlers(runtime).complete_build(
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
        return await _install_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
    ) -> ToolResponse:
        """Enqueue the boot job for an installed Run; poll jobs.* for completion. Operator only."""
        return await _boot_run(pool, current_context(), run_id)
