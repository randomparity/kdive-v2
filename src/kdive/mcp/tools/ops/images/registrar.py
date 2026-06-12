"""Operator/admin ``images.*`` MCP tool registration (M2.4/7, ADR-0092/0093, issue #288).

Each workflow owns its authorization and audit shape:

* ``build_publish``: platform-operator public image build/publish job admission.
* ``upload``: project-scoped private image registration from quarantine.
* ``delete``: project-scoped private image deletion with the shared reference guard.
* ``retention``: platform-admin break-glass prune/extend operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from kdive.domain.errors import CategorizedError
from kdive.jobs.payloads import ImageBuildPayload
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.ops.images._common import (
    DELETE_TOOL,
    EXTEND_TOOL,
    PRUNE_OBJECT_ID,
    PRUNE_TOOL,
    UPLOAD_TOOL,
)
from kdive.mcp.tools.ops.images.build_publish import BUILD_TOOL, PUBLISH_TOOL, build, publish
from kdive.mcp.tools.ops.images.delete import delete
from kdive.mcp.tools.ops.images.retention import extend, prune_expired
from kdive.mcp.tools.ops.images.upload import upload
from kdive.reconciler.images import ImageSweepStore
from kdive.services.images.upload import UploadObjectStore

if TYPE_CHECKING:
    from kdive.store.objectstore import ObjectStore


class ImageBuildRequest(BaseModel):
    """MCP-facing public image build/publish request shared by both tools."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(description="The provider whose plane builds or built the image.")
    name: str = Field(description="The catalog image name.")
    arch: str = Field(description="The target architecture.")
    releasever: str = Field(description="The distro release version.")
    source_image_digest: str = Field(description="The base image content digest.")
    capabilities: tuple[str, ...] = Field(
        default=(), description="The guest-contract tags the image must satisfy."
    )
    format: Literal["qcow2"] = Field(default="qcow2", description="The image format.")
    root_device: str = Field(default="/dev/vda", description="The guest root device path.")

    def to_payload(self) -> ImageBuildPayload:
        """Convert the MCP request into the durable IMAGE_BUILD job payload."""
        return ImageBuildPayload(
            provider=self.provider,
            name=self.name,
            arch=self.arch,
            releasever=self.releasever,
            source_image_digest=self.source_image_digest,
            capabilities=self.capabilities,
            format=self.format,
            root_device=self.root_device,
        )


def _resolve_object_store() -> ObjectStore | None:
    """Resolve the shared S3 object store from ``KDIVE_S3_*``, or ``None`` if unconfigured."""
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
    """Register the ``images.*`` operator/admin tools on ``app``, bound to ``pool``."""

    @app.tool(name=BUILD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_build(
        request: Annotated[
            ImageBuildRequest,
            Field(description="Public image build request."),
        ],
    ) -> ToolResponse:
        """Enqueue an IMAGE_BUILD job for a public base image. Requires platform_operator."""
        return await build(pool, current_context(), payload=request.to_payload())

    @app.tool(name=PUBLISH_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def images_publish(
        request: Annotated[
            ImageBuildRequest,
            Field(description="Public image publish request."),
        ],
    ) -> ToolResponse:
        """Promote a built image to a public catalog row. Requires platform_operator."""
        return await publish(pool, current_context(), payload=request.to_payload())

    @app.tool(name=UPLOAD_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
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
        name=DELETE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_delete(
        image_id: Annotated[str, Field(description="The private catalog image to delete.")],
    ) -> ToolResponse:
        """Delete a project-private image. Requires operator on the image's project."""
        return await delete(pool, current_context(), image_id=image_id)

    @app.tool(name=PRUNE_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"})
    async def images_prune_expired(
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Force the expired-private-image sweep now. Requires platform_admin."""
        if image_store is None:
            return _config_error(PRUNE_OBJECT_ID)
        return await prune_expired(pool, current_context(), reason=reason, image_store=image_store)

    @app.tool(
        name=EXTEND_TOOL, annotations=_docmeta.destructive(), meta={"maturity": "implemented"}
    )
    async def images_extend(
        image_id: Annotated[str, Field(description="The private image whose lifetime to extend.")],
        seconds: Annotated[int, Field(description="Seconds from now (clamped to the ceiling).")],
        reason: Annotated[
            str, Field(description="Mandatory non-blank break-glass justification (audited).")
        ],
    ) -> ToolResponse:
        """Re-arm a private image's expiry. Requires platform_admin."""
        return await extend(
            pool, current_context(), image_id=image_id, seconds=seconds, reason=reason
        )


__all__ = [
    "ImageBuildRequest",
    "register",
    "register_from_env",
]
