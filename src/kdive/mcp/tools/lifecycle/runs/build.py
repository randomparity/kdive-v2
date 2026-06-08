"""`runs.build` and `runs.complete_build` MCP handlers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.catalog import load_fixture_catalog
from kdive.components.references import ComponentRef
from kdive.components.requirements import ConfigRequirements
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, Run, Sensitivity
from kdive.domain.state import RunState
from kdive.jobs import queue
from kdive.jobs.payloads import BuildPayload
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import authorizing as job_authorizing
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools.lifecycle.runs.common import (
    RUN_BUILD_TERMINAL,
    run_job_envelope,
)
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.providers.build_validation import validate_external_artifacts
from kdive.providers.component_validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.providers.ports import BuildOutput, ValidatedUpload
from kdive.security import audit
from kdive.security.context import RequestContext
from kdive.security.rbac import Role, require_role
from kdive.services.run_steps import BuildStepResult, platform_owned_cmdline_token
from kdive.services.run_steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import (
    HeadResult,
    StoredArtifact,
    object_store_from_env,
    register_artifact_row,
)

type ConfigValidator = Callable[[ComponentRef], None]


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
        profile_requirements: ConfigRequirements | None = None,
    ) -> ValidatedUpload: ...


class StoreBackedValidator:
    """Default validator: builds an ObjectStore from env and runs the provider validator."""

    def validate(
        self,
        run_id: UUID,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
        profile_requirements: ConfigRequirements | None = None,
    ) -> ValidatedUpload:
        store = object_store_from_env()
        return validate_external_artifacts(
            store,
            manifest=manifest,
            keys=keys,
            declared_build_id=declared_build_id,
            profile_requirements=profile_requirements,
        )


@dataclass(frozen=True, slots=True)
class RunBuildHandlers:
    """Handlers with provider validation seams bound by the registrar or test fixture."""

    component_sources: ComponentSourceCapabilities
    config_validator: ConfigValidator | None = None
    complete_validator: CompleteBuildValidator = field(default_factory=StoreBackedValidator)

    async def build_run(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        cmdline: str | None = None,
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
                    return _config_error(
                        run_id, data={"reason": "external_source_uses_complete_build"}
                    )
                try:
                    reject_unsupported_component_source(
                        self.component_sources,
                        component_kind="config",
                        ref=parsed.config,
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure(run_id, exc.category)
                if self.config_validator is not None:
                    try:
                        self.config_validator(parsed.config)
                    except CategorizedError as exc:
                        return ToolResponse.failure(run_id, exc.category)
                return await _build_locked(conn, ctx, run, cmdline)

    async def complete_build(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        run_id: str,
        *,
        build_id: str | None,
        cmdline: str,
    ) -> ToolResponse:
        """Validate an external Run's uploads and finalize it ``created -> succeeded``."""
        return await _complete_build(
            pool,
            ctx,
            run_id,
            build_id=build_id,
            cmdline=cmdline,
            validator=self.complete_validator,
        )


async def _complete_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    run_id: str,
    *,
    build_id: str | None,
    cmdline: str,
    validator: CompleteBuildValidator,
) -> ToolResponse:
    """Validate an external Run's uploads and finalize it ``created -> succeeded``."""
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
                profile = _external_build_profile(run)
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)
            guard = _complete_build_guard(run, profile)
            if guard is not None:
                return guard

            manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
            if manifest_row is None:
                return _config_error(run_id, data={"reason": "no_upload_manifest"})
            keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}

            try:
                requirements = _external_config_requirements(profile)
                validated = await asyncio.to_thread(
                    validator.validate,
                    uid,
                    list(manifest_row.entries),
                    keys,
                    build_id,
                    requirements,
                )
            except CategorizedError as exc:
                return ToolResponse.failure(run_id, exc.category)

            return await _finalize_external_build(
                conn, ctx, run, validated.output, cmdline, keys, validated.heads
            )


def _external_build_profile(run: Run) -> ExternalBuildProfile:
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ExternalBuildProfile):
        raise CategorizedError(
            "run is not an external build",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed


def _complete_build_guard(run: Run, profile: ExternalBuildProfile) -> ToolResponse | None:
    """Reject a non-external or non-CREATED Run; ``None`` means proceed to finalize."""
    _ = profile
    if run.state is not RunState.CREATED:
        return _config_error(str(run.id), data={"current_status": run.state.value})
    return None


def _external_config_requirements(profile: ExternalBuildProfile) -> ConfigRequirements | None:
    if profile.profile_requirements is None:
        return None
    entry = load_fixture_catalog().profile(
        profile.profile_requirements.provider,
        profile.profile_requirements.name,
    )
    if entry is None:
        raise CategorizedError(
            "unknown build profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return entry.requires.config


def _complete_envelope(run_id: UUID, result: BuildStepResult) -> ToolResponse:
    """Build the success envelope from a ledger ``result``."""
    return ToolResponse.success(
        str(run_id), "succeeded", suggested_next_actions=["runs.get"], refs=result.refs()
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
    result = BuildStepResult(
        kernel_ref=output.kernel_ref,
        debuginfo_ref=output.debuginfo_ref,
        initrd_ref=keys.get("initrd"),
        build_id=output.build_id,
        cmdline=cmdline,
    )
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
            (run.id, Jsonb(result.dump())),
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
