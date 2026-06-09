"""Registrar for the `runs.*` MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._runtime_resolution import runtime_for_run
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


@dataclass(frozen=True, slots=True)
class _RunRuntimeFactory:
    pool: AsyncConnectionPool
    resolver: ProviderResolver

    async def for_run(self, run_id: str) -> ProviderRuntime | ToolResponse:
        return await runtime_for_run(self.pool, self.resolver, run_id)

    def build_handlers(self, runtime: ProviderRuntime) -> _RunBuildHandlers:
        return _RunBuildHandlers(
            runtime.component_sources,
            config_validator=runtime.build_config_validator,
        )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver | None = None,
) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""
    if resolver is None:
        raise RuntimeError("runs registrar requires an injected provider resolver")
    runtime_factory = _RunRuntimeFactory(pool, resolver)
    _register_runs_get(app, pool)
    _register_runs_create(app, pool)
    _register_runs_build(app, pool, runtime_factory)
    _register_runs_complete_build(app, pool, runtime_factory)
    _register_runs_install(app, pool)
    _register_runs_boot(app, pool)


def _register_runs_get(app: FastMCP, pool: AsyncConnectionPool) -> None:
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


def _register_runs_create(app: FastMCP, pool: AsyncConnectionPool) -> None:
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
        reuse_requirement: Annotated[
            _RunReuseRequirementInput | None,
            Field(
                description=(
                    "Optional System reuse assertion payload with vcpus, memory_gb, "
                    "disk_gb, and pcie fields. Omit to skip extra reuse matching."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Bind a Run to a ready System and Investigation in one transaction. Requires operator."""
        request = _RunCreateRequest(
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
            expected_boot_failure=expected_boot_failure,
            reuse_requirement=reuse_requirement,
        )
        return await _create_run(pool, current_context(), request)


def _register_runs_build(
    app: FastMCP, pool: AsyncConnectionPool, runtime_factory: _RunRuntimeFactory
) -> None:
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
        runtime = await runtime_factory.for_run(run_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await runtime_factory.build_handlers(runtime).build_run(
            pool,
            current_context(),
            run_id,
            cmdline=cmdline,
        )


def _register_runs_complete_build(
    app: FastMCP, pool: AsyncConnectionPool, runtime_factory: _RunRuntimeFactory
) -> None:
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
        runtime = await runtime_factory.for_run(run_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await runtime_factory.build_handlers(runtime).complete_build(
            pool, current_context(), run_id, build_id=build_id, cmdline=cmdline
        )


def _register_runs_install(app: FastMCP, pool: AsyncConnectionPool) -> None:
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


def _register_runs_boot(app: FastMCP, pool: AsyncConnectionPool) -> None:
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
