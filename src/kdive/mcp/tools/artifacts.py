"""The `artifacts.*` MCP tools — redacted-only artifact reads (ADR-0031).

`artifacts.list(system_id)` and `artifacts.get(artifact_id)` surface **only** `redacted`
rows; a `sensitive` artifact id is shaped as not-found, so the raw vmcore is never
fetchable through the agent surface even by id. Project membership is enforced through the
owning System.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Annotated, Any, LiteralString, Protocol
from uuid import UUID

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.domain.state import RunState, SystemState
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.security.rbac import Role, require_role
from kdive.store.objectstore import (
    PresignedUpload,
    artifact_key,
    object_store_from_env,
    owner_prefix,
)

_log = logging.getLogger(__name__)

_TENANT = "local"
_BUILD_ARTIFACT_NAMES = frozenset({"kernel", "initrd", "vmlinux"})
_ROOTFS_NAME = "rootfs"
_RETENTION_CLASS = "build"
_DEFAULT_UPLOAD_TTL_SECONDS = 86400
# The single presigned PUT caps at 5 GiB on real S3, so a larger declared size would mint
# a PUT the store rejects mid-upload. Uploads above this need multipart/split — see #112.
_DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024


def _upload_ttl() -> timedelta:
    return timedelta(
        seconds=int(os.environ.get("KDIVE_UPLOAD_TTL_SECONDS", _DEFAULT_UPLOAD_TTL_SECONDS))
    )


def _max_upload_bytes() -> int:
    return int(os.environ.get("KDIVE_MAX_UPLOAD_BYTES", _DEFAULT_MAX_UPLOAD_BYTES))


def _presign_ttl_seconds() -> int:
    return min(3600, int(_upload_ttl().total_seconds()))


class _PresignStore(Protocol):
    def presign_put(
        self,
        key: str,
        *,
        sha256: str,
        size_bytes: int,
        sensitivity: Sensitivity,
        retention_class: str,
        expires_in: int,
    ) -> PresignedUpload: ...


def _allowed_names(owner_kind: str) -> frozenset[str]:
    return _BUILD_ARTIFACT_NAMES if owner_kind == "run" else frozenset({_ROOTFS_NAME})


def _validate_artifact_declarations(
    object_id: str, artifacts: list[dict[str, Any]], allowed: frozenset[str], cap: int
) -> list[ManifestEntry] | ToolResponse:
    """Validate the declared artifacts, returning the entries or a config-error envelope.

    Returns the parsed :class:`ManifestEntry` list on success, or a
    ``CONFIGURATION_ERROR`` :class:`ToolResponse` if any declaration is malformed, a size
    is out of range, or the set is empty.
    """
    entries: list[ManifestEntry] = []
    for art in artifacts:
        name, sha256, size = art.get("name"), art.get("sha256"), art.get("size_bytes")
        if name not in allowed or not isinstance(sha256, str) or not isinstance(size, int):
            return _config_error(object_id, data={"reason": "bad_artifact_declaration"})
        if size <= 0 or size > cap:
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size))
    if not entries:
        return _config_error(object_id, data={"reason": "no_artifacts_declared"})
    return entries


async def _owner_accepts_upload(conn: AsyncConnection, owner_kind: str, owner_id: UUID) -> bool:
    """True iff the owner is in its pre-upload state (CREATED external Run / DEFINED System)."""
    if owner_kind == "run":
        run = await RUNS.get(conn, owner_id)
        if run is None or run.state is not RunState.CREATED:
            return False
        parsed = BuildProfile.parse(run.build_profile)
        return isinstance(parsed, ExternalBuildProfile)
    # A System opens a rootfs-upload window only in DEFINED with an upload-kind rootfs;
    # the provisioning plane commits it at provisioning->ready (#111, ADR-0048 §5/§6).
    system = await SYSTEMS.get(conn, owner_id)
    if system is None or system.state is not SystemState.DEFINED:
        return False
    parsed = ProvisioningProfile.parse(system.provisioning_profile)
    return parsed.provider.local_libvirt.rootfs.kind == "upload"


async def _owner_project(conn: AsyncConnection, kind: str, owner_id: UUID) -> str | None:
    table = "runs" if kind == "runs" else "systems"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT project FROM {table} WHERE id = %s", (owner_id,))  # noqa: S608 - 2-value whitelist
        row = await cur.fetchone()
    return row["project"] if row else None


_LIST_SQL: LiteralString = (
    "SELECT id, object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND sensitivity = 'redacted' "
    "ORDER BY created_at DESC"
)
_GET_SQL: LiteralString = (
    "SELECT id, object_key, owner_id FROM artifacts "
    "WHERE id = %s AND owner_kind = 'systems' AND sensitivity = 'redacted'"
)
_PROJECT_SQL: LiteralString = "SELECT project FROM systems WHERE id = %s"


def _config_error(object_id: str, *, data: dict[str, str] | None = None) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR, data=data or {})


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def artifacts_list(
    pool: AsyncConnectionPool, ctx: RequestContext, *, system_id: str
) -> list[ToolResponse]:
    """Return the System's `redacted` artifacts as envelopes (empty list if none/absent)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return []
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_PROJECT_SQL, (uid,))
            owner = await cur.fetchone()
            if owner is None or owner["project"] not in ctx.projects:
                return []
            await cur.execute(_LIST_SQL, (uid,))
            rows = await cur.fetchall()
    responses: list[ToolResponse] = []
    for row in rows:
        try:
            responses.append(
                ToolResponse.success(
                    str(row["id"]),
                    "available",
                    suggested_next_actions=["artifacts.get"],
                    refs={"object": row["object_key"]},
                )
            )
        except ValueError:
            _log.warning("artifact %s violates the envelope invariant; degraded", row["id"])
    return responses


async def artifacts_get(
    pool: AsyncConnectionPool, ctx: RequestContext, *, artifact_id: str
) -> ToolResponse:
    """Return one `redacted` artifact's envelope, or a not-found-shaped config error.

    A missing artifact and a `sensitive` artifact are indistinguishable (both
    `configuration_error`), so the raw vmcore cannot be fetched even when its id is known.
    """
    uid = _as_uuid(artifact_id)
    if uid is None:
        return _config_error(artifact_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_GET_SQL, (uid,))
            row = await cur.fetchone()
            if row is None:
                return _config_error(artifact_id)
            await cur.execute(_PROJECT_SQL, (row["owner_id"],))
            owner = await cur.fetchone()
        if owner is None or owner["project"] not in ctx.projects:
            return _config_error(artifact_id)
        return ToolResponse.success(
            artifact_id,
            "available",
            suggested_next_actions=["artifacts.get"],
            refs={"object": row["object_key"]},
        )


async def create_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    owner_kind: str,
    owner_id: str,
    artifacts: list[dict[str, Any]],
    store: _PresignStore | None = None,
) -> list[ToolResponse]:
    """Mint a presigned PUT per declared artifact and persist the owner's manifest.

    Replaces the owner's manifest with the declared set (one call, full set). Returns one
    envelope per artifact; an error returns a single failure envelope. Requires operator.
    """
    store = store or object_store_from_env()
    uid = _as_uuid(owner_id)
    if uid is None or owner_kind not in ("run", "system"):
        return [_config_error(owner_id)]
    kind = "runs" if owner_kind == "run" else "systems"
    # The 'system' arm is the DEFINED rootfs-upload lane: create the window with
    # systems.define, upload here, then systems.provision admits it and commits the rootfs.
    next_action = "runs.complete_build" if owner_kind == "run" else "systems.provision"

    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            project = await _owner_project(conn, kind, uid)
            if project is None or project not in ctx.projects:
                return [_config_error(owner_id)]
            require_role(ctx, project, Role.OPERATOR)

            allowed = _allowed_names(owner_kind)
            validated = _validate_artifact_declarations(
                owner_id, artifacts, allowed, _max_upload_bytes()
            )
            if isinstance(validated, ToolResponse):
                return [validated]
            entries = validated

            prefix = owner_prefix(_TENANT, kind, str(uid))
            lock_scope = LockScope.RUN if owner_kind == "run" else LockScope.SYSTEM
            try:
                async with conn.transaction(), advisory_xact_lock(conn, lock_scope, uid):
                    if not await _owner_accepts_upload(conn, owner_kind, uid):
                        return [
                            _config_error(owner_id, data={"reason": "owner_not_accepting_upload"})
                        ]
                    uploads = [
                        (
                            entry,
                            artifact_key(_TENANT, kind, str(uid), entry.name),
                            store.presign_put(
                                artifact_key(_TENANT, kind, str(uid), entry.name),
                                sha256=entry.sha256,
                                size_bytes=entry.size_bytes,
                                sensitivity=Sensitivity.SENSITIVE,
                                retention_class=_RETENTION_CLASS,
                                expires_in=_presign_ttl_seconds(),
                            ),
                        )
                        for entry in entries
                    ]
                    await upload_manifest.replace_manifest(
                        conn,
                        owner_kind=kind,
                        owner_id=uid,
                        prefix=prefix,
                        entries=entries,
                        ttl=_upload_ttl(),
                    )
            except CategorizedError as exc:
                # A corrupt stored profile, presign, or manifest-write failure prevents (and
                # rolls back) the manifest write; surface it so the cause is observable.
                _log.warning("create_upload failed for %s %s: %s", owner_kind, owner_id, exc)
                return [ToolResponse.failure(owner_id, exc.category)]

    return [
        ToolResponse.success(
            key,
            "upload_ready",
            suggested_next_actions=[next_action],
            refs={"upload_url": presigned.url},
            data={
                "name": entry.name,
                "expires_in": str(_presign_ttl_seconds()),
                **presigned.required_headers,
            },
        )
        for entry, key, presigned in uploads
    ]


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `artifacts.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="artifacts.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def artifacts_list_tool(
        system_id: Annotated[
            str, Field(description="The System whose redacted artifacts to list.")
        ],
    ) -> list[ToolResponse]:
        """List the redacted artifacts for a System. Requires project membership."""
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
        """Fetch one redacted artifact by id; sensitive ids are not-found (no raw vmcore leak)."""
        return await artifacts_get(pool, current_context(), artifact_id=artifact_id)

    @app.tool(
        name="artifacts.create_upload",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def artifacts_create_upload_tool(
        owner_kind: Annotated[
            str, Field(description="'run' (build artifacts) or 'system' (rootfs).")
        ],
        owner_id: Annotated[str, Field(description="The owning Run or System id.")],
        artifacts: Annotated[
            list[dict[str, Any]],
            Field(description="Declared artifacts: [{name, sha256 (base64), size_bytes}]."),
        ],
    ) -> list[ToolResponse]:
        """Mint presigned PUTs for an owner's declared artifacts. Requires operator."""
        return await create_upload(
            pool, current_context(), owner_kind=owner_kind, owner_id=owner_id, artifacts=artifacts
        )
