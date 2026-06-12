"""Operator/admin ``images.*`` MCP tools (M2.4/7, ADR-0092/0093, issue #288).

The image-management surface for the ``kdivectl images`` operator CLI. Every verb shares the
worker/job/reconciler service layer — there is no second source of truth. The role each verb
needs is the spec's authz table, and the platform-role order is **not** a total hierarchy
(``platform_admin`` does not imply ``platform_operator``), so the routine operator verbs and the
destructive admin break-glass verbs are distinct gates:

* ``images.build`` / ``images.publish`` authorize ``platform_operator`` and enqueue an
  ``IMAGE_BUILD`` job (the shared build → guest-contract-validate → row-first publish handler).
* ``images.delete`` is **project-scoped**: it resolves the image's owning project from the row,
  then requires ``operator`` on that project. A member-over-reach or cross-project caller is
  denied and audited (``record_denial``) before the row is touched; the catalog row survives.
* ``images.prune_expired`` / ``images.extend`` route the M1.3 **break-glass** path
  (``platform_admin``), not the per-allocation destructive gate. ``prune_expired`` runs the
  reconciler's reference-guarded + extend-fenced expired-private sweep; ``extend`` re-arms one
  private image's ``expires_at`` under the per-row lock (clamped to the lifetime ceiling).

Each gate writes its accountability row **before** mutating any pool state: a platform-role
denial that holds a platform role records a ``platform_audit_log`` row, a project-role denial
records an ``audit_log`` denial row, and every authorized mutation records its success row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

import kdive.config as config
from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_DEFAULT,
    IMAGE_PRIVATE_LIFETIME_MAX,
)
from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageCatalogEntry, ImageVisibility, JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import ImageBuildPayload
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.ops._auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.reconciler.images import (
    ImageSweepStore,
    image_referenced_by_live_system,
    repair_expired_private_images,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    AuthorizationError,
    PlatformRole,
    Role,
    require_platform_role,
    require_role,
)
from kdive.services.images.upload import (
    PrivateUploadRequest,
    UploadObjectStore,
    register_private_upload,
)

if TYPE_CHECKING:
    from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

_BUILD_TOOL = "images.build"
_PUBLISH_TOOL = "images.publish"
_UPLOAD_TOOL = "images.upload"
_DELETE_TOOL = "images.delete"
_DEFAULT_REQUIRED_CONTRACT = ("agent", "kdump", "drgn")
# The published catalog object prefix. A quarantine key under it would let an operator
# re-ingest another project's already-published (owner-scoped) private image into their own
# catalog, so it is rejected — an upload sources only a freshly-quarantined object.
_PUBLISHED_IMAGE_PREFIX = "images/"
_PRUNE_TOOL = "images.prune_expired"
_EXTEND_TOOL = "images.extend"
_OBJECT_KIND = "image_catalog"
# Public base-image builds are a platform action, not a project's — the job's authorizing
# project is a sentinel for attribution only (the handler reads no project from it).
_PLATFORM_PROJECT = "platform"
_PRUNE_OBJECT_ID = "expired-private"
_PRUNE_SCOPE = "all-private"


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


def _blank(reason: str) -> bool:
    return not reason.strip()


# --- build / publish: platform_operator -----------------------------------------------------


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
            job_authorizing(ctx, _PLATFORM_PROJECT),
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
        pool, ctx, tool=_BUILD_TOOL, payload=payload, object_id=payload.name
    )


async def publish(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Promote a built image to a public catalog row via ``IMAGE_BUILD``. Requires operator.

    Shares the build handler's row-first publish two-write (build → validate → publish), so a
    realized ``defined`` baseline and a fresh build land through the one publish path; there is
    no second promote implementation.
    """
    return await _operator_image_build(
        pool, ctx, tool=_PUBLISH_TOOL, payload=payload, object_id=payload.name
    )


# --- upload: project-scoped operator role ---------------------------------------------------


def _default_expiry(now: datetime) -> datetime:
    """The default private-image TTL deadline (clamped later by the upload service ceiling)."""
    return now + timedelta(seconds=config.require(IMAGE_PRIVATE_LIFETIME_DEFAULT))


async def _audit_project_denial(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    project: str,
    args: dict[str, object],
) -> None:
    """Record a project-role denial row on its own connection before any pool mutation."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_denial(
            conn,
            event=audit.DenialEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                tool=tool,
                args=args,
                reason=f"{ctx.principal!r} may not {tool} in project {project!r}",
            ),
        )


async def upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    store: UploadObjectStore | None,
    *,
    project: str,
    name: str,
    arch: str,
    quarantine_key: str,
    lifetime_seconds: int | None,
) -> ToolResponse:
    """Register a quarantined upload as a project-private image. Requires ``operator`` on it.

    Gates ``operator`` on ``project`` first (a member-over-reach or cross-project caller is
    denied and audited before the store is read — the authz boundary is evaluated even when no
    object store is configured), then delegates to the shared :func:`register_private_upload`,
    which enforces the per-project quota fail-closed under the project lock, validates the guest
    contract, and publishes through the row-first two-write. ``lifetime_seconds`` (clamped to the
    ceiling by the service) defaults to the configured private-image lifetime when absent.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_role(ctx, project, Role.OPERATOR)
        except AuthorizationError:
            await _audit_project_denial(
                pool, ctx, tool=_UPLOAD_TOOL, project=project, args={"name": name}
            )
            return _denied(name, _UPLOAD_TOOL)
        if store is None:  # No KDIVE_S3_* configured — authz already evaluated above.
            return _config_error(name)
        if quarantine_key.startswith(_PUBLISHED_IMAGE_PREFIX):
            # Reject a source key in the published catalog prefix: it would let an operator
            # re-ingest another project's owner-scoped private image into their own catalog.
            return _config_error(name, data={"reason": "quarantine_key in published prefix"})
        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(seconds=lifetime_seconds)
            if lifetime_seconds is not None
            else _default_expiry(now)
        )
        return await _register_upload(
            pool,
            ctx,
            store,
            project=project,
            name=name,
            arch=arch,
            quarantine_key=quarantine_key,
            expires_at=expires_at,
        )


async def _register_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    store: UploadObjectStore,
    *,
    project: str,
    name: str,
    arch: str,
    quarantine_key: str,
    expires_at: datetime,
) -> ToolResponse:
    """Delegate to the shared upload service; map its typed errors to an envelope."""
    async with pool.connection() as conn:
        try:
            entry: ImageCatalogEntry = await register_private_upload(
                conn,
                store,
                request=PrivateUploadRequest(
                    project=project,
                    principal=ctx.principal,
                    name=name,
                    provider="local-libvirt",
                    arch=arch,
                    quarantine_key=quarantine_key,
                    expires_at=expires_at,
                    required=_DEFAULT_REQUIRED_CONTRACT,
                ),
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(name, exc)
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={"name": entry.name, "visibility": entry.visibility.value, "owner": project},
    )


# --- delete: project-scoped operator role ---------------------------------------------------


async def delete(pool: AsyncConnectionPool, ctx: RequestContext, *, image_id: str) -> ToolResponse:
    """Delete a project-private catalog image. Requires ``operator`` on the image's project.

    Resolves the image's owning project from the row, then gates ``operator`` on it. A
    member-over-reach (a viewer) or cross-project caller is denied and audited (a ``denied``
    ``audit_log`` row attributed to the image's project) before the row is touched; the catalog
    row survives. Routing a cross-project force-delete for an operator is the ``platform_admin``
    break-glass path, deliberately not exposed here (the per-project gate is the only deletion
    path on this tool).
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _config_error(image_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            entry = await IMAGE_CATALOG.get(conn, uid)
        if entry is None:
            return _config_error(image_id)
        if entry.visibility is not ImageVisibility.PRIVATE or entry.owner is None:
            return _config_error(image_id)
        try:
            require_role(ctx, entry.owner, Role.OPERATOR)
        except AuthorizationError:
            await _audit_project_denial(
                pool, ctx, tool=_DELETE_TOOL, project=entry.owner, args={"image_id": image_id}
            )
            return _denied(image_id, _DELETE_TOOL)
        return await _delete_owned(pool, ctx, uid, project=entry.owner)


async def _delete_owned(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID, *, project: str
) -> ToolResponse:
    """Reference-guard, then delete the row + audit, all under the row's ``FOR UPDATE`` lock.

    The row lock spans the JSONB-containment reference probe and the delete, so a System
    provisioned to reference this image after the resolve read is observed and the delete
    declines rather than orphaning that System's rootfs (the same guard the reconciler's
    expired-prune uses — one source of truth). A referenced image returns a typed
    ``CONFIGURATION_ERROR``; the row survives. The object is left for the reconciler's
    dangling/leaked sweep to GC (row-first removal: a rowless object is never stranded).
    """
    async with (
        pool.connection() as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM image_catalog WHERE id = %s AND visibility = %s FOR UPDATE",
            (uid, ImageVisibility.PRIVATE.value),
        )
        if await cur.fetchone() is None:  # A concurrent delete won; desired end state holds.
            return ToolResponse.success(str(uid), "deleted")
        if await image_referenced_by_live_system(cur, uid):
            return ToolResponse.failure(
                str(uid),
                ErrorCategory.CONFIGURATION_ERROR,
                data={"reason": "image is referenced by a non-terminal System"},
            )
        await cur.execute("DELETE FROM image_catalog WHERE id = %s", (uid,))
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=_DELETE_TOOL,
                object_kind=_OBJECT_KIND,
                object_id=uid,
                transition="deleted",
                args={"image_id": str(uid)},
                project=project,
            ),
        )
    _log.info("operator %s deleted private image %s in project %s", ctx.principal, uid, project)
    return ToolResponse.success(str(uid), "deleted")


# --- prune_expired / extend: platform_admin break-glass -------------------------------------


async def _record_admin_breakglass(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    scope: str,
    args: dict[str, object],
) -> None:
    """Write the always-on ``platform_audit_log`` accountability row in its own transaction."""
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=tool,
                scope=scope,
                args=args,
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )


async def prune_expired(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    reason: str,
    image_store: ImageSweepStore,
) -> ToolResponse:
    """Force the reconciler's expired-private-image sweep now. Requires ``platform_admin``.

    Gates ``platform_admin`` first (a denial writes no audit-of-success row and never touches the
    store), records the break-glass accountability row, then runs the **same** reference-guarded +
    extend-fenced sweep the periodic reconciler runs — a referenced or freshly-extended image is
    not pruned. Returns the number of images pruned.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
        except AuthorizationError:
            await audit_platform_denial(
                pool, ctx, tool=_PRUNE_TOOL, scope=f"denied:{_PRUNE_SCOPE}", args={}
            )
            return _denied(_PRUNE_OBJECT_ID, _PRUNE_TOOL)
        if _blank(reason):
            return _config_error(_PRUNE_OBJECT_ID)
        await _record_admin_breakglass(
            pool, ctx, tool=_PRUNE_TOOL, scope=_PRUNE_SCOPE, args={"reason": reason}
        )
        async with pool.connection() as conn:
            pruned = await repair_expired_private_images(conn, image_store)
        _log.warning("break-glass prune of %d expired private images by %s", pruned, ctx.principal)
        return ToolResponse.success(_PRUNE_OBJECT_ID, "pruned", data={"pruned": str(pruned)})


def _ceiling(now: datetime) -> datetime:
    """The per-image lifetime ceiling: ``now`` plus the configured maximum lifetime."""
    return now + timedelta(seconds=config.require(IMAGE_PRIVATE_LIFETIME_MAX))


async def extend(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    image_id: str,
    seconds: int,
    reason: str,
) -> ToolResponse:
    """Re-arm a private image's ``expires_at`` under the per-row lock. Requires ``platform_admin``.

    Gates ``platform_admin`` first (a denial writes no extension), records the break-glass row,
    then sets ``expires_at = now() + seconds`` clamped to the lifetime ceiling under the same
    ``FOR UPDATE`` lock the reconciler's extend fence honors, so a concurrent prune sees the
    re-armed deadline. ``seconds`` must be positive.
    """
    uid = _as_uuid(image_id)
    if uid is None:
        return _config_error(image_id)
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_EXTEND_TOOL,
                scope=f"denied:{image_id}",
                args={"image_id": image_id},
            )
            return _denied(image_id, _EXTEND_TOOL)
        if _blank(reason) or seconds <= 0:
            return _config_error(image_id)
        await _record_admin_breakglass(
            pool,
            ctx,
            tool=_EXTEND_TOOL,
            scope=f"image:{image_id}",
            args={"image_id": image_id, "seconds": str(seconds), "reason": reason},
        )
        return await _rearm_expiry(pool, uid, seconds=seconds)


async def _rearm_expiry(pool: AsyncConnectionPool, uid: UUID, *, seconds: int) -> ToolResponse:
    """Set the private image's ``expires_at`` to the clamped deadline under its row lock."""
    async with (
        pool.connection() as conn,
        conn.transaction(),
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            "SELECT id FROM image_catalog "
            "WHERE id = %s AND visibility = %s AND expires_at IS NOT NULL FOR UPDATE",
            (uid, ImageVisibility.PRIVATE.value),
        )
        if await cur.fetchone() is None:
            return _config_error(str(uid))
        now = datetime.now(UTC)
        requested = now + timedelta(seconds=seconds)
        deadline = min(requested, _ceiling(now))
        await cur.execute("UPDATE image_catalog SET expires_at = %s WHERE id = %s", (deadline, uid))
    return ToolResponse.success(str(uid), "extended", data={"expires_at": deadline.isoformat()})


def _resolve_object_store() -> ObjectStore | None:
    """Resolve the shared S3 object store from ``KDIVE_S3_*``, or ``None`` if unconfigured.

    One ``ObjectStore`` satisfies both the sweep (``ImageSweepStore``) and upload
    (``UploadObjectStore``) ports, so the prune and upload tools share the same backing store.
    Mirrors the reconciler's wiring: no S3 env leaves both store-backed tools fail-closed.
    """
    from kdive.store.objectstore import object_store_from_env

    try:
        return object_store_from_env()
    except CategorizedError:
        return None


def register_from_env(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the ``images.*`` tools, resolving the object store from the environment."""
    store = _resolve_object_store()
    register(app, pool, image_store=store, upload_store=store)


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    image_store: ImageSweepStore | None,
    upload_store: UploadObjectStore | None = None,
) -> None:
    """Register the ``images.*`` operator/admin tools on ``app``, bound to ``pool``.

    ``image_store`` is the expired-private sweep store and ``upload_store`` the private-upload
    object store; both are ``None`` when ``KDIVE_S3_*`` is unconfigured — then
    ``images.prune_expired`` and ``images.upload`` return a configuration error (no store),
    mirroring how the reconciler skips the image sweeps without S3.
    """

    @app.tool(name=_BUILD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_build(
        provider: Annotated[str, Field(description="The provider whose plane builds the image.")],
        name: Annotated[str, Field(description="The catalog image name.")],
        arch: Annotated[str, Field(description="The target architecture.")],
        releasever: Annotated[str, Field(description="The distro release version to build.")],
        source_image_digest: Annotated[str, Field(description="The base image content digest.")],
        capabilities: Annotated[
            list[str], Field(description="The guest-contract tags the image must satisfy.")
        ],
        format: Annotated[
            Literal["qcow2"], Field(description="The only supported image format.")
        ] = "qcow2",
        root_device: Annotated[str, Field(description="The guest root device path.")] = "/dev/vda",
    ) -> ToolResponse:
        """Enqueue an IMAGE_BUILD job for a public base image. Requires platform_operator."""
        return await build(
            pool,
            current_context(),
            payload=ImageBuildPayload(
                provider=provider,
                name=name,
                arch=arch,
                releasever=releasever,
                source_image_digest=source_image_digest,
                capabilities=tuple(capabilities),
                format=format,
                root_device=root_device,
            ),
        )

    @app.tool(name=_PUBLISH_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_publish(
        provider: Annotated[str, Field(description="The provider whose plane built the image.")],
        name: Annotated[str, Field(description="The catalog image name.")],
        arch: Annotated[str, Field(description="The target architecture.")],
        releasever: Annotated[str, Field(description="The distro release version.")],
        source_image_digest: Annotated[str, Field(description="The base image content digest.")],
        capabilities: Annotated[
            list[str], Field(description="The guest-contract tags the image must satisfy.")
        ],
        format: Annotated[
            Literal["qcow2"], Field(description="The only supported image format.")
        ] = "qcow2",
        root_device: Annotated[str, Field(description="The guest root device path.")] = "/dev/vda",
    ) -> ToolResponse:
        """Promote a built image to a public catalog row. Requires platform_operator."""
        return await publish(
            pool,
            current_context(),
            payload=ImageBuildPayload(
                provider=provider,
                name=name,
                arch=arch,
                releasever=releasever,
                source_image_digest=source_image_digest,
                capabilities=tuple(capabilities),
                format=format,
                root_device=root_device,
            ),
        )

    @app.tool(name=_UPLOAD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_upload(
        project: Annotated[str, Field(description="The owning project for the private image.")],
        name: Annotated[str, Field(description="The catalog image name.")],
        arch: Annotated[str, Field(description="The target architecture.")],
        quarantine_key: Annotated[
            str, Field(description="The object-store key of the quarantined upload.")
        ],
        lifetime_seconds: Annotated[
            int | None, Field(description="TTL seconds (clamped to the ceiling); default applies.")
        ] = None,
    ) -> ToolResponse:
        """Register a quarantined upload as a project-private image. Requires operator."""
        return await upload(
            pool,
            current_context(),
            upload_store,
            project=project,
            name=name,
            arch=arch,
            quarantine_key=quarantine_key,
            lifetime_seconds=lifetime_seconds,
        )

    @app.tool(
        name=_DELETE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_delete(
        image_id: Annotated[str, Field(description="The private catalog image to delete.")],
    ) -> ToolResponse:
        """Delete a project-private image. Requires operator on the image's project."""
        return await delete(pool, current_context(), image_id=image_id)

    @app.tool(
        name=_PRUNE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_prune_expired(
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Force the expired-private-image sweep now. Requires platform_admin (break-glass)."""
        if image_store is None:
            return _config_error(_PRUNE_OBJECT_ID)
        return await prune_expired(pool, current_context(), reason=reason, image_store=image_store)

    @app.tool(
        name=_EXTEND_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_extend(
        image_id: Annotated[str, Field(description="The private image whose lifetime to extend.")],
        seconds: Annotated[int, Field(description="Seconds from now (clamped to the ceiling).")],
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Re-arm a private image's expiry. Requires platform_admin (break-glass)."""
        return await extend(
            pool, current_context(), image_id=image_id, seconds=seconds, reason=reason
        )
