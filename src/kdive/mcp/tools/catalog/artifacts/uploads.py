"""Artifact upload-admission handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import NamedTuple, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import MAX_UPLOAD_BYTES, UPLOAD_TTL_SECONDS
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError
from kdive.domain.models import Sensitivity
from kdive.domain.state import RunState, SystemState
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.profiles.provider_policy import rootfs_upload_window_allowed
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.provider_components.artifacts import PresignedUpload, PresignPutRequest
from kdive.provider_components.uploads import ManifestEntry
from kdive.providers.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.store.objectstore import (
    artifact_key,
    object_store_from_env,
    owner_prefix,
)

_log = logging.getLogger(__name__)

_TENANT = "local"
_BUILD_ARTIFACT_NAMES = frozenset({"effective_config", "kernel", "initrd", "vmlinux"})
_ROOTFS_NAME = "rootfs"
_RETENTION_CLASS = "build"
_EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES = 1024 * 1024


def _upload_ttl() -> timedelta:
    return timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))


def _max_upload_bytes() -> int:
    return config.require(MAX_UPLOAD_BYTES)


def _presign_ttl_seconds() -> int:
    return min(3600, int(_upload_ttl().total_seconds()))


class _PresignStore(Protocol):
    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...


class _MaterializedUpload(NamedTuple):
    entry: ManifestEntry
    key: str
    presigned: PresignedUpload


type ArtifactDeclaration = Mapping[str, object]
"""Raw MCP declaration for one artifact upload before value validation."""


@dataclass(frozen=True)
class _UploadOwnerSpec:
    owner_kind: str
    lock_scope: LockScope
    allowed_names: frozenset[str]
    next_action: str
    project: Callable[[AsyncConnection, UUID], Awaitable[str | None]]
    accepts_upload: Callable[[AsyncConnection, UUID, ProviderResolver | None], Awaitable[bool]]


def _validate_artifact_declarations(
    object_id: str, artifacts: Sequence[ArtifactDeclaration], allowed: frozenset[str], cap: int
) -> list[ManifestEntry] | ToolResponse:
    entries: list[ManifestEntry] = []
    for art in artifacts:
        try:
            name, sha256, size = art["name"], art["sha256"], art["size_bytes"]
        except KeyError:
            return _config_error(object_id, data={"reason": "bad_artifact_declaration"})
        if (
            not isinstance(name, str)
            or name not in allowed
            or not isinstance(sha256, str)
            or not isinstance(size, int)
        ):
            return _config_error(object_id, data={"reason": "bad_artifact_declaration"})
        artifact_cap = _EFFECTIVE_CONFIG_MAX_UPLOAD_BYTES if name == "effective_config" else cap
        if size <= 0 or size > artifact_cap:
            return _config_error(object_id, data={"reason": "size_out_of_range"})
        entries.append(ManifestEntry(name=name, sha256=sha256, size_bytes=size))
    if not entries:
        return _config_error(object_id, data={"reason": "no_artifacts_declared"})
    return entries


def _materialize_uploads(
    entries: list[ManifestEntry],
    *,
    kind: str,
    owner_id: UUID,
    store: _PresignStore,
) -> list[_MaterializedUpload]:
    uploads: list[_MaterializedUpload] = []
    expires_in = _presign_ttl_seconds()
    for entry in entries:
        key = artifact_key(_TENANT, kind, str(owner_id), entry.name)
        presigned = store.presign_put(
            PresignPutRequest(
                key=key,
                sha256=entry.sha256,
                size_bytes=entry.size_bytes,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
                expires_in=expires_in,
            )
        )
        uploads.append(_MaterializedUpload(entry, key, presigned))
    return uploads


async def _run_project(conn: AsyncConnection, owner_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT project FROM runs WHERE id = %s", (owner_id,))
        row = await cur.fetchone()
    return row["project"] if row else None


async def _system_project(conn: AsyncConnection, owner_id: UUID) -> str | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT project FROM systems WHERE id = %s", (owner_id,))
        row = await cur.fetchone()
    return row["project"] if row else None


async def _run_accepts_upload(
    conn: AsyncConnection, owner_id: UUID, _resolver: ProviderResolver | None
) -> bool:
    run = await RUNS.get(conn, owner_id)
    if run is None or run.state is not RunState.CREATED:
        return False
    parsed = BuildProfile.parse(run.build_profile)
    return isinstance(parsed, ExternalBuildProfile)


async def _system_accepts_upload(
    conn: AsyncConnection, owner_id: UUID, resolver: ProviderResolver | None
) -> bool:
    if resolver is None:
        raise RuntimeError("system upload admission requires a resolver")
    system = await SYSTEMS.get(conn, owner_id)
    if system is None or system.state is not SystemState.DEFINED:
        return False
    parsed = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, owner_id)
    return rootfs_upload_window_allowed(runtime.profile_policy, parsed)


_RUN_UPLOAD = _UploadOwnerSpec(
    owner_kind="runs",
    lock_scope=LockScope.RUN,
    allowed_names=_BUILD_ARTIFACT_NAMES,
    next_action="runs.complete_build",
    project=_run_project,
    accepts_upload=_run_accepts_upload,
)
_SYSTEM_UPLOAD = _UploadOwnerSpec(
    owner_kind="systems",
    lock_scope=LockScope.SYSTEM,
    allowed_names=frozenset({_ROOTFS_NAME}),
    next_action="systems.provision_defined",
    project=_system_project,
    accepts_upload=_system_accepts_upload,
)


async def _create_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    spec: _UploadOwnerSpec,
    owner_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    store: _PresignStore | None = None,
    resolver: ProviderResolver | None = None,
) -> ToolResponse:
    store = store or object_store_from_env()
    uid = _as_uuid(owner_id)
    if uid is None:
        return _config_error(owner_id)

    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            project = await spec.project(conn, uid)
            if project is None or project not in ctx.projects:
                return _config_error(owner_id)
            require_role(ctx, project, Role.OPERATOR)

            validated = _validate_artifact_declarations(
                owner_id, artifacts, spec.allowed_names, _max_upload_bytes()
            )
            if isinstance(validated, ToolResponse):
                return validated
            entries = validated

            prefix = owner_prefix(_TENANT, spec.owner_kind, str(uid))
            try:
                async with conn.transaction(), advisory_xact_lock(conn, spec.lock_scope, uid):
                    if not await spec.accepts_upload(conn, uid, resolver):
                        return _config_error(
                            owner_id, data={"reason": "owner_not_accepting_upload"}
                        )
                    uploads = _materialize_uploads(
                        entries,
                        kind=spec.owner_kind,
                        owner_id=uid,
                        store=store,
                    )
                    await upload_manifest.replace_manifest(
                        conn,
                        upload_manifest.UploadManifestReplaceRequest(
                            owner_kind=spec.owner_kind,
                            owner_id=uid,
                            prefix=prefix,
                            entries=entries,
                            ttl=_upload_ttl(),
                        ),
                    )
            except CategorizedError as exc:
                _log.warning("create_upload failed for %s %s: %s", spec.owner_kind, owner_id, exc)
                return ToolResponse.failure_from_error(owner_id, exc)

    items = [_upload_response(upload, next_action=spec.next_action) for upload in uploads]
    return ToolResponse.collection(
        owner_id,
        "upload_ready",
        items,
        suggested_next_actions=[spec.next_action],
        data={"owner_kind": spec.owner_kind},
    )


def _upload_response(upload: _MaterializedUpload, *, next_action: str) -> ToolResponse:
    return ToolResponse.success(
        upload.key,
        "upload_ready",
        suggested_next_actions=[next_action],
        refs={"upload_url": upload.presigned.url},
        data={
            "name": upload.entry.name,
            "expires_in": str(_presign_ttl_seconds()),
            **upload.presigned.required_headers,
        },
    )


async def create_run_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    run_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    store: _PresignStore | None = None,
) -> ToolResponse:
    """Mint presigned PUTs for an external Run's declared build artifacts."""
    return await _create_upload(
        pool,
        ctx,
        spec=_RUN_UPLOAD,
        owner_id=run_id,
        artifacts=artifacts,
        store=store,
    )


async def create_system_upload(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    system_id: str,
    artifacts: Sequence[ArtifactDeclaration],
    resolver: ProviderResolver,
    store: _PresignStore | None = None,
) -> ToolResponse:
    """Mint presigned PUTs for a DEFINED System's uploaded rootfs."""
    return await _create_upload(
        pool,
        ctx,
        spec=_SYSTEM_UPLOAD,
        owner_id=system_id,
        artifacts=artifacts,
        store=store,
        resolver=resolver,
    )
