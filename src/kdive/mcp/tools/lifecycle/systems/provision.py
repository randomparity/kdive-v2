"""System define/provision admission handlers (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.jobs.handlers.systems``.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle.systems.view import defined_system_envelope
from kdive.profiles.types import ProvisioningProfileInput
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.security.authz.context import RequestContext
from kdive.services.systems.admission import (
    AdmissionFailure,
    AdmissionResult,
    CreateSystemRequest,
    DefinedSystemAdmitted,
    ProvisionDefinedRequest,
    ProvisionJobAdmitted,
    SystemAdmission,
)
from kdive.services.systems.validation import RootfsValidator


def _admission_response(result: AdmissionResult) -> ToolResponse:
    if isinstance(result, AdmissionFailure):
        return ToolResponse.failure(
            result.object_id,
            result.category,
            suggested_next_actions=list(result.suggested_next_actions),
            data=result.data,
        )
    if isinstance(result, ProvisionJobAdmitted):
        return job_envelope(result.job, "system_id", result.system_id)
    if isinstance(result, DefinedSystemAdmitted):
        return defined_system_envelope(result.system)
    raise TypeError(f"unknown system admission result: {type(result).__name__}")


@dataclass(frozen=True, slots=True)
class SystemProvisionHandlers:
    """Provisioning handlers with provider validation seams bound at construction."""

    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    def _admission(self) -> SystemAdmission:
        return SystemAdmission(self.component_sources, self.rootfs_validator)

    async def provision_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
    ) -> ToolResponse:
        """Mint a System for a ``granted`` Allocation and enqueue its provision job."""
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        with bind_context(principal=ctx.principal):
            result = await self._admission().create_for_allocation(
                pool,
                ctx,
                CreateSystemRequest(allocation_id=uid, profile=profile, mode="provision"),
            )
        return _admission_response(result)

    async def provision_defined_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
    ) -> ToolResponse:
        """Admit a ``defined`` System after its upload window is complete."""
        uid = _as_uuid(system_id)
        if uid is None:
            return _config_error(system_id)
        with bind_context(principal=ctx.principal):
            result = await self._admission().provision_defined(
                pool,
                ctx,
                ProvisionDefinedRequest(system_id=uid),
            )
        return _admission_response(result)

    async def define_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
    ) -> ToolResponse:
        """Create a System in ``defined`` for a ``granted`` Allocation."""
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        with bind_context(principal=ctx.principal):
            result = await self._admission().create_for_allocation(
                pool,
                ctx,
                CreateSystemRequest(allocation_id=uid, profile=profile, mode="define"),
            )
        return _admission_response(result)
