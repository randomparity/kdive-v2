"""The public `artifacts.*` MCP tool registrar."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.catalog.artifacts_reads import (
    ArtifactReadHandlers,
    ArtifactSearchRequest,
    artifacts_get,
    artifacts_list,
)
from kdive.mcp.tools.catalog.artifacts_uploads import (
    ArtifactDeclaration,
    create_run_upload,
    create_system_upload,
)

__all__ = [
    "artifacts_get",
    "artifacts_list",
    "ArtifactSearchRequest",
    "ArtifactReadHandlers",
    "create_run_upload",
    "create_system_upload",
    "register",
]


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""
    read_handlers = ArtifactReadHandlers()

    @app.tool(
        name="artifacts.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def artifacts_list_tool(
        system_id: Annotated[
            str, Field(description="The System whose redacted artifacts to list.")
        ],
    ) -> ToolResponse:
        """List the redacted artifacts for a System. Requires viewer."""
        return await artifacts_list(pool, current_context(), system_id=system_id)

    @app.tool(
        name="artifacts.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def artifacts_get_tool(
        artifact_id: Annotated[
            str,
            Field(description="The redacted artifact to fetch (sensitive ids are not-found)."),
        ],
    ) -> ToolResponse:
        """Fetch one redacted artifact by id. Requires viewer; sensitive ids are not-found."""
        return await artifacts_get(pool, current_context(), artifact_id=artifact_id)

    @app.tool(
        name="artifacts.search_text",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def artifacts_search_text_tool(
        artifact_id: Annotated[str, Field(description="The redacted System artifact id.")],
        pattern: Annotated[
            str,
            Field(description="Literal OR search pattern, e.g. '__d_lookup' or 'panic'."),
        ],
        before_lines: Annotated[int, Field(description="Context lines before each match.")] = 2,
        after_lines: Annotated[int, Field(description="Context lines after each match.")] = 4,
        max_matches: Annotated[int, Field(description="Maximum match windows to return.")] = 20,
    ) -> ToolResponse:
        """Search a redacted System artifact with bounded literal line context."""
        return await read_handlers.artifacts_search_text(
            pool,
            current_context(),
            request=ArtifactSearchRequest(
                artifact_id=artifact_id,
                pattern=pattern,
                before_lines=before_lines,
                after_lines=after_lines,
                max_matches=max_matches,
            ),
        )

    @app.tool(
        name="artifacts.create_run_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_run_upload_tool(
        run_id: Annotated[str, Field(description="The external-build Run id.")],
        artifacts: Annotated[
            list[ArtifactDeclaration],
            Field(description="Declared build artifacts: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> ToolResponse:
        """Mint presigned PUTs for an external Run's build artifacts. Requires operator."""
        return await create_run_upload(pool, current_context(), run_id=run_id, artifacts=artifacts)

    @app.tool(
        name="artifacts.create_system_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_system_upload_tool(
        system_id: Annotated[str, Field(description="The DEFINED System id.")],
        artifacts: Annotated[
            list[ArtifactDeclaration],
            Field(description="Declared rootfs artifact: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> ToolResponse:
        """Mint a presigned PUT for a DEFINED System's rootfs. Requires operator."""
        return await create_system_upload(
            pool, current_context(), system_id=system_id, artifacts=artifacts
        )
