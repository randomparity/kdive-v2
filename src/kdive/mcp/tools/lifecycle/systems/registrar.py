"""Registrar for the `systems.*` MCP tools."""

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
from kdive.mcp.tools.lifecycle.systems.admin import (
    SystemAdminHandlers as _SystemAdminHandlers,
)
from kdive.mcp.tools.lifecycle.systems.admin import (
    teardown_system as _teardown_system,
)
from kdive.mcp.tools.lifecycle.systems.provision import (
    SystemProvisionHandlers as _SystemProvisionHandlers,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    get_system as _get_system,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    list_systems as _list_systems,
)
from kdive.profiles.types import ProvisioningProfileInput
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime


def register(
    app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver | None = None
) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""
    if resolver is None:
        raise RuntimeError("systems registrar requires an injected provider resolver")

    async def _runtime_for_allocation(allocation_id: str) -> ProviderRuntime | ToolResponse:
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        async with pool.connection() as conn:
            try:
                return await resolver.runtime_for_allocation(conn, uid)
            except CategorizedError as exc:
                return ToolResponse.failure(allocation_id, exc.category)

    async def _runtime_for_system(system_id: str) -> ProviderRuntime | ToolResponse:
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        async with pool.connection() as conn:
            try:
                return await resolver.runtime_for_system(conn, uid)
            except CategorizedError as exc:
                return ToolResponse.failure(system_id, exc.category)

    def _provision_handlers(runtime: ProviderRuntime) -> _SystemProvisionHandlers:
        rootfs_validator = _rootfs_validator(runtime)
        return _SystemProvisionHandlers(runtime.component_sources, rootfs_validator)

    def _admin_handlers(runtime: ProviderRuntime) -> _SystemAdminHandlers:
        rootfs_validator = _rootfs_validator(runtime)
        return _SystemAdminHandlers(runtime.component_sources, rootfs_validator)

    def _rootfs_validator(runtime: ProviderRuntime):
        if runtime.rootfs_validator is None:
            raise RuntimeError("systems registrar requires an injected rootfs validator")
        return runtime.rootfs_validator

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
            ProvisioningProfileInput,
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation (upload window). Operator only."""
        runtime = await _runtime_for_allocation(allocation_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _provision_handlers(runtime).define_system(
            pool,
            current_context(),
            allocation_id=allocation_id,
            profile=profile,
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
            ProvisioningProfileInput,
            Field(description="Provisioning profile for the System create lane."),
        ],
    ) -> ToolResponse:
        """Mint a System for a granted Allocation and enqueue provision. Operator only."""
        runtime = await _runtime_for_allocation(allocation_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _provision_handlers(runtime).provision_system(
            pool,
            current_context(),
            allocation_id=allocation_id,
            profile=profile,
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
        runtime = await _runtime_for_system(system_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _provision_handlers(runtime).provision_defined_system(
            pool,
            current_context(),
            system_id=system_id,
        )

    @app.tool(
        name="systems.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_get(
        system_id: Annotated[str, Field(description="The System to render.")],
    ) -> ToolResponse:
        """Return a System the caller can view."""
        return await _get_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_list(
        allocation_id: Annotated[
            str | None, Field(description="Only Systems under this Allocation id.")
        ] = None,
        state: Annotated[
            str | None, Field(description="Only Systems in this lifecycle state.")
        ] = None,
        shape: Annotated[
            str | None,
            Field(
                description="Only Systems with this named shape, or '__custom__' for "
                "full-custom (no shape)."
            ),
        ] = None,
        pcie: Annotated[
            str | None,
            Field(
                description="Only Systems whose Allocation claims a device matching this "
                "'<vendor>:<device>' spec."
            ),
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = _DEFAULT_LIST_LIMIT,
    ) -> ToolResponse:
        """List the caller's Systems, filterable by allocation/state/shape/PCIe. Requires viewer."""
        return await _list_systems(
            pool,
            current_context(),
            allocation_id=allocation_id,
            state=state,
            shape=shape,
            pcie=pcie,
            limit=limit,
        )

    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
    ) -> ToolResponse:
        """Enqueue teardown for a System. Requires admin and destructive-op opt-in."""
        return await _teardown_system(pool, current_context(), system_id)

    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            ProvisioningProfileInput,
            Field(description="New provisioning profile; must opt in to reprovision."),
        ],
    ) -> ToolResponse:
        """Enqueue in-place reprovision for a ready System. Requires operator and opt-in."""
        runtime = await _runtime_for_system(system_id)
        if isinstance(runtime, ToolResponse):
            return runtime
        return await _admin_handlers(runtime).reprovision_system(
            pool,
            current_context(),
            system_id=system_id,
            profile=profile,
        )
