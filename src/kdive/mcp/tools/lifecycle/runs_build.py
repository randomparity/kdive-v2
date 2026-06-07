"""`runs.build` and `runs.complete_build` MCP handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError
from kdive.domain.models import Job, JobKind, Run, Sensitivity
from kdive.domain.state import RunState
from kdive.jobs import queue
from kdive.jobs.payloads import BuildPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs_common import (
    RUN_BUILD_TERMINAL,
    run_job_envelope,
)
from kdive.planes.runs_shared import existing_build_result as _existing_build_result
from kdive.planes.runs_shared import platform_owned_cmdline_token
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.providers.build_validation import validate_external_artifacts
from kdive.providers.ports import BuildOutput, ValidatedUpload
from kdive.security import audit
from kdive.security.context import RequestContext
from kdive.security.rbac import Role, require_role
from kdive.store.objectstore import (
    HeadResult,
    StoredArtifact,
    object_store_from_env,
    register_artifact_row,
)


async def build_run(
    pool: AsyncConnectionPool, ctx: RequestContext, run_id: str, *, cmdline: str | None = None
) -> ToolResponse:
    """Admit an idempotent server build for a Run and enqueue the build job."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    owned = platform_owned_cmdline_token(cmdline)
    if owned is not None:
        return _config_error(
            run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            try:
                parsed = BuildProfile.parse(run.build_profile)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)
            if parsed.source != "server":
                return _config_error(run_id, data={"reason": "external_source_uses_complete_build"})
            return await _build_locked(conn, ctx, run, cmdline)


async def _build_locked(
    conn: AsyncConnection, ctx: RequestContext, run: Run, cmdline: str | None
) -> ToolResponse:
    """Admit the build under the per-Run lock: flip `created -> running`, then enqueue."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state in RUN_BUILD_TERMINAL:
            return _config_error(str(run.id), data={"current_status": state.value})
        if state is RunState.CREATED:
            await conn.execute(
                "UPDATE runs SET state = 'running' WHERE id = %s AND state = 'created'", (run.id,)
            )
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool="runs.build",
                    object_kind="runs",
                    object_id=run.id,
                    transition="created->running",
                    args={"run_id": str(run.id)},
                    project=run.project,
                ),
            )
        job = await _enqueue_build(conn, ctx, run, cmdline)
    return run_job_envelope(job, run.id)


async def _enqueue_build(
    conn: AsyncConnection, ctx: RequestContext, run: Run, cmdline: str | None
) -> Job:
    payload = BuildPayload(run_id=str(run.id), cmdline=cmdline if cmdline else None)
    return await queue.enqueue(
        conn,
        JobKind.BUILD,
        payload,
        job_authorizing(ctx, run.project),
        f"{run.id}:build",
    )


class CompleteBuildValidator(Protocol):
    """Validator seam for external build uploads."""

    def validate(
        self,
        run_id: UUID,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
    ) -> ValidatedUpload: ...


class StoreBackedValidator:
    """Default validator: builds an ObjectStore from env and runs the provider validator."""

    def validate(
        self,
        run_id: UUID,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
    ) -> ValidatedUpload:
        store = object_store_from_env()
        return validate_external_artifacts(
            store, manifest=manifest, keys=keys, declared_build_id=declared_build_id
        )


async def complete_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    build_id: str | None,
    cmdline: str,
    validator: CompleteBuildValidator | None = None,
) -> ToolResponse:
    """Validate an external Run's uploads and finalize it ``created -> succeeded``."""
    validator = validator or StoreBackedValidator()
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    owned = platform_owned_cmdline_token(cmdline)
    if owned is not None:
        return _config_error(
            run_id, data={"reason": "cmdline_overrides_platform_args", "token": owned}
        )
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)

            recorded = await _existing_build_result(conn, uid)
            if recorded is not None:
                return _complete_envelope(uid, recorded)

            try:
                guard = _complete_build_guard(run)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)
            if guard is not None:
                return guard

            manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
            if manifest_row is None:
                return _config_error(run_id, data={"reason": "no_upload_manifest"})
            keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}

            try:
                validated = await asyncio.to_thread(
                    validator.validate, uid, list(manifest_row.entries), keys, build_id
                )
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)

            return await _finalize_external_build(
                conn, ctx, run, validated.output, cmdline, keys, validated.heads
            )


def _complete_build_guard(run: Run) -> ToolResponse | None:
    """Reject a non-external or non-CREATED Run; ``None`` means proceed to finalize."""
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ExternalBuildProfile):
        return _config_error(str(run.id), data={"reason": "not_external_source"})
    if run.state is not RunState.CREATED:
        return _config_error(str(run.id), data={"current_status": run.state.value})
    return None


def _complete_envelope(run_id: UUID, result: dict[str, Any]) -> ToolResponse:
    """Build the success envelope from a ledger ``result``."""
    refs = {"kernel": result["kernel_ref"]}
    if result.get("debuginfo_ref"):
        refs["vmlinux"] = result["debuginfo_ref"]
    if result.get("initrd_ref"):
        refs["initrd"] = result["initrd_ref"]
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=["runs.get"], refs=refs
    )


async def _finalize_external_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    output: BuildOutput,
    cmdline: str,
    keys: dict[str, str],
    heads: dict[str, HeadResult],
) -> ToolResponse:
    """Write artifact rows, ledger result, and created -> succeeded under the per-Run lock."""
    result = {
        "kernel_ref": output.kernel_ref,
        "debuginfo_ref": output.debuginfo_ref,
        "initrd_ref": keys.get("initrd", ""),
        "build_id": output.build_id,
        "cmdline": cmdline,
    }
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state is RunState.SUCCEEDED:
            recorded = await _existing_build_result(conn, run.id)
            return _complete_envelope(run.id, recorded or result)
        if state is not RunState.CREATED:
            return _config_error(str(run.id), data={"current_status": state.value})
        for name, head in heads.items():
            stored = StoredArtifact(keys[name], head.etag, Sensitivity.SENSITIVE, "build")
            await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="runs", owner_id=run.id)
            )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result)),
        )
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'created'",
            (output.kernel_ref, output.debuginfo_ref or None, run.id),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.complete_build",
                object_kind="runs",
                object_id=run.id,
                transition="created->succeeded",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
        await upload_manifest.delete_manifest(conn, "runs", run.id)
    return _complete_envelope(run.id, result)
