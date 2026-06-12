"""``images.upload`` project-private image workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

import kdive.config as config
from kdive.config.core_settings import IMAGE_PRIVATE_LIFETIME_DEFAULT
from kdive.domain.errors import CategorizedError
from kdive.domain.models import ImageCatalogEntry
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.ops.images._common import UPLOAD_TOOL, audit_project_denial, denied
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, Role, RoleDenied, require_role
from kdive.services.images.upload import (
    PrivateUploadRequest,
    UploadObjectStore,
    register_private_upload,
)

DEFAULT_REQUIRED_CONTRACT = ("agent", "kdump", "drgn")
# The published catalog object prefix. A quarantine key under it would let an operator
# re-ingest another project's already-published (owner-scoped) private image into their own
# catalog, so it is rejected: an upload sources only a freshly-quarantined object.
PUBLISHED_IMAGE_PREFIX = "images/"


class ImageUploadRequest(BaseModel):
    """MCP-facing private image upload registration request."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(description="The owning project for the private image.")
    name: str = Field(description="The catalog image name.")
    arch: str = Field(description="The target architecture.")
    quarantine_key: str = Field(description="The object-store key of the quarantined upload.")
    lifetime_seconds: int | None = Field(
        default=None,
        description="TTL seconds (clamped to the ceiling); default applies.",
    )


def _default_expiry(now: datetime) -> datetime:
    """The default private-image TTL deadline (clamped later by the upload service ceiling)."""
    return now + timedelta(seconds=config.require(IMAGE_PRIVATE_LIFETIME_DEFAULT))


async def upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    store: UploadObjectStore | None,
    request: ImageUploadRequest,
) -> ToolResponse:
    """Register a quarantined upload as a project-private image. Requires ``operator`` on it.

    Gates ``operator`` on ``project`` first (a member-over-reach or cross-project caller is
    denied and audited before the store is read, so authz is evaluated even when no object store
    is configured), then delegates to :func:`register_private_upload`. The service enforces the
    per-project quota fail-closed under the project lock, validates the guest contract, and
    publishes through the row-first two-write. ``lifetime_seconds`` defaults to the configured
    private-image lifetime when absent, then the service clamps it to the ceiling.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_role(ctx, request.project, Role.OPERATOR)
        except RoleDenied:
            await audit_project_denial(
                pool, ctx, tool=UPLOAD_TOOL, project=request.project, args={"name": request.name}
            )
            return denied(request.name, UPLOAD_TOOL)
        except AuthorizationError:
            return denied(request.name, UPLOAD_TOOL)
        if store is None:
            return _config_error(request.name)
        if request.quarantine_key.startswith(PUBLISHED_IMAGE_PREFIX):
            return _config_error(
                request.name, data={"reason": "quarantine_key in published prefix"}
            )
        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(seconds=request.lifetime_seconds)
            if request.lifetime_seconds is not None
            else _default_expiry(now)
        )
        return await _register_upload(
            pool,
            ctx,
            store,
            request=request,
            expires_at=expires_at,
        )


async def _register_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    store: UploadObjectStore,
    *,
    request: ImageUploadRequest,
    expires_at: datetime,
) -> ToolResponse:
    """Delegate to the shared upload service; map its typed errors to an envelope."""
    async with pool.connection() as conn:
        try:
            entry: ImageCatalogEntry = await register_private_upload(
                conn,
                store,
                request=PrivateUploadRequest(
                    project=request.project,
                    principal=ctx.principal,
                    name=request.name,
                    provider="local-libvirt",
                    arch=request.arch,
                    quarantine_key=request.quarantine_key,
                    expires_at=expires_at,
                    required=DEFAULT_REQUIRED_CONTRACT,
                ),
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(request.name, exc)
    return ToolResponse.success(
        str(entry.id),
        entry.state.value,
        data={
            "name": entry.name,
            "visibility": entry.visibility.value,
            "owner": request.project,
        },
    )
