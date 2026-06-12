"""``images.build`` and ``images.publish`` platform-operator workflow."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import ImageBuildPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

BUILD_TOOL = "images.build"
PUBLISH_TOOL = "images.publish"
PLATFORM_PROJECT = "platform"


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


async def _enqueue_image_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Audit the operator action, then enqueue the shared ``IMAGE_BUILD`` job idempotently.

    The dedup key is the image identity so a re-issued build/publish returns the same job
    rather than enqueuing a duplicate. The ``platform_audit_log`` accountability row is written
    in the same transaction as the enqueue (both commit or neither does).
    """
    dedup_key = f"image_build:{payload.provider}:{payload.name}:{payload.arch}"
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=f"{payload.provider}:{payload.name}",
                args={"provider": payload.provider, "name": payload.name, "arch": payload.arch},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )
        job = await queue.enqueue(
            conn,
            JobKind.IMAGE_BUILD,
            payload,
            job_authorizing(ctx, PLATFORM_PROJECT),
            dedup_key,
        )
    return ToolResponse.success(
        str(job.id),
        job.state.value,
        suggested_next_actions=["jobs.get", "jobs.wait"],
        refs={"job": str(job.id)},
        data={"kind": job.kind.value, "name": payload.name},
    )


async def _operator_image_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    payload: ImageBuildPayload,
    object_id: str,
) -> ToolResponse:
    """Gate ``platform_operator`` first (a denial writes no job), then enqueue the build."""
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=tool,
                scope=f"denied:{object_id}",
                args={"name": payload.name},
            )
            return _denied(object_id, tool)
        return await _enqueue_image_build(pool, ctx, tool=tool, payload=payload)


async def build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Enqueue an ``IMAGE_BUILD`` job for a public base image. Requires ``platform_operator``."""
    return await _operator_image_build(
        pool, ctx, tool=BUILD_TOOL, payload=payload, object_id=payload.name
    )


async def publish(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Promote a built image to a public catalog row via ``IMAGE_BUILD``. Requires operator.

    Shares the build handler's row-first publish two-write (build -> validate -> publish), so a
    realized ``defined`` baseline and a fresh build land through the one publish path; there is
    no second promote implementation.
    """
    return await _operator_image_build(
        pool, ctx, tool=PUBLISH_TOOL, payload=payload, object_id=payload.name
    )
