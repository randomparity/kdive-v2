"""`runs.build` and `runs.complete_build` MCP handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.build_configs.defaults import DEFAULT_CONFIG_REF
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS
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
from kdive.profiles.build import BuildProfile, ExternalBuildProfile, ServerBuildProfile
from kdive.provider_components.artifacts import HeadResult, StoredArtifact, chunk_key
from kdive.provider_components.build_results import BuildOutput, ValidatedUpload
from kdive.provider_components.build_validation import validate_external_artifacts
from kdive.provider_components.catalog import load_fixture_catalog
from kdive.provider_components.reassembly import reassemble_chunked
from kdive.provider_components.references import CONFIG_COMPONENT, ComponentRef
from kdive.provider_components.requirements import ConfigRequirements
from kdive.provider_components.uploads import ManifestEntry
from kdive.provider_components.validation import (
    ComponentSourceCapabilities,
    reject_unsupported_component_source,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.build_host_selection import resolve_and_admit
from kdive.services.runs.steps import BuildStepResult, platform_owned_cmdline_token
from kdive.services.runs.steps import existing_build_result as _existing_build_result
from kdive.store.objectstore import (
    object_store_from_env,
    register_artifact_row,
)

_log = logging.getLogger(__name__)


class ExternalBuildStore(Protocol):
    """Object-store surface the external-build finalize path needs (ADR-0104).

    A superset of ``ValidatorStore`` (head + get_range) plus ``delete`` and the four multipart
    primitives, so one factory return type covers validation, reassembly, and chunk cleanup.
    The concrete :class:`~kdive.store.objectstore.ObjectStore` satisfies it.
    """

    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str: ...
    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str: ...
    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


type ConfigValidator = Callable[[ComponentRef], None]
type CompleteBuildValidation = Callable[
    [Sequence[ManifestEntry], Mapping[str, str], str | None, ConfigRequirements | None],
    ValidatedUpload,
]
type ObjectStoreFactory = Callable[[], ExternalBuildStore]


async def _build_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    parsed_profile: ServerBuildProfile,
) -> ToolResponse:
    """Admit the build under the per-Run lock: select host, flip `created -> running`, enqueue.

    Host selection and capacity admission run FIRST inside the transaction so that a
    capacity failure raises before any state mutation; the transaction rolls back on any
    exception, leaving no lease, no job, and no state change.

    Args:
        conn: An async psycopg connection (no active transaction yet).
        ctx: The request authorization context.
        run: The Run being admitted for build.
        cmdline: Optional caller-supplied extra kernel cmdline tokens.
        parsed_profile: The pre-validated server-build profile (avoids re-parsing).

    Returns:
        A :class:`~kdive.mcp.responses.ToolResponse` — queued on success, or a failure
        envelope when the Run is in a terminal state or host admission fails.
    """
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            host = await resolve_and_admit(conn, parsed_profile, run.id)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
                row = await cur.fetchone()
            if row is None:
                raise CategorizedError(str(run.id), category=ErrorCategory.CONFIGURATION_ERROR)
            state = RunState(row["state"])
            if state in RUN_BUILD_TERMINAL:
                raise CategorizedError(
                    str(run.id),
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"current_status": state.value},
                )
            if state is RunState.CREATED:
                await conn.execute(
                    "UPDATE runs SET state = %s WHERE id = %s AND state = %s",
                    (RunState.RUNNING.value, run.id, RunState.CREATED.value),
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
            job = await _enqueue_build(conn, ctx, run, cmdline, host_id=str(host.id))
    except CategorizedError as exc:
        next_actions = ["runs.build"] if exc.category is ErrorCategory.CAPACITY_EXHAUSTED else None
        return ToolResponse.failure_from_error(
            str(run.id), exc, suggested_next_actions=next_actions
        )
    return run_job_envelope(job, run.id)


async def _enqueue_build(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    cmdline: str | None,
    *,
    host_id: str,
) -> Job:
    payload = BuildPayload(
        run_id=str(run.id),
        cmdline=cmdline if cmdline else None,
        build_host_id=host_id,
    )
    return await queue.enqueue(
        conn,
        JobKind.BUILD,
        payload,
        job_authorizing(ctx, run.project),
        f"{run.id}:build",
    )


@dataclass(frozen=True, slots=True)
class RunBuildHandlers:
    """Handlers with provider validation seams bound by the registrar or test fixture."""

    component_sources: ComponentSourceCapabilities
    config_validator: ConfigValidator | None = None
    validate_complete_build: CompleteBuildValidation | None = None
    object_store_factory: ObjectStoreFactory = object_store_from_env

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
                    return ToolResponse.failure_from_error(run_id, exc)
                if not isinstance(parsed, ServerBuildProfile):
                    return _config_error(
                        run_id, data={"reason": "external_source_uses_complete_build"}
                    )
                # An omitted config validates against the kdump catalog default — the same
                # substitution the resolver applies on the build path (ADR-0096), so a provider
                # that does not accept a catalog config rejects an omitted ref at run-creation.
                config_ref = parsed.config or DEFAULT_CONFIG_REF
                try:
                    reject_unsupported_component_source(
                        self.component_sources,
                        component_kind=CONFIG_COMPONENT,
                        ref=config_ref,
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)
                if self.config_validator is not None:
                    try:
                        self.config_validator(config_ref)
                    except CategorizedError as exc:
                        return ToolResponse.failure_from_error(run_id, exc)
                return await _build_locked(conn, ctx, run, cmdline, parsed)

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
                    return ToolResponse.failure_from_error(run_id, exc)
                guard = _created_run_guard(run)
                if guard is not None:
                    return guard

                manifest_row = await upload_manifest.get_manifest(conn, "runs", uid)
                if manifest_row is None:
                    return _config_error(run_id, data={"reason": "no_upload_manifest"})
                has_chunks = any(e.chunks is not None for e in manifest_row.entries)
                keys = {e.name: f"{manifest_row.prefix}{e.name}" for e in manifest_row.entries}
                store = self.object_store_factory() if has_chunks else None
                if store is not None:
                    guard = await _reassemble_chunked_artifacts(
                        conn, uid, run_id, manifest_row, store
                    )
                    if guard is not None:
                        return guard

                try:
                    requirements = _external_config_requirements(profile)
                    validated = await asyncio.to_thread(
                        self._validate_complete_build,
                        list(manifest_row.entries),
                        keys,
                        build_id,
                        requirements,
                    )
                except CategorizedError as exc:
                    return ToolResponse.failure_from_error(run_id, exc)

                return await _finalize_external_build(
                    conn,
                    ctx,
                    run,
                    validated.output,
                    cmdline,
                    keys,
                    validated.heads,
                    store=store,
                    entries=manifest_row.entries,
                    prefix=manifest_row.prefix,
                    chunked=has_chunks,
                )

    def _validate_complete_build(
        self,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
        profile_requirements: ConfigRequirements | None,
    ) -> ValidatedUpload:
        if self.validate_complete_build is not None:
            return self.validate_complete_build(
                manifest, keys, declared_build_id, profile_requirements
            )
        return validate_external_artifacts(
            self.object_store_factory(),
            manifest=manifest,
            keys=keys,
            declared_build_id=declared_build_id,
            profile_requirements=profile_requirements,
        )


async def _reassemble_chunked_artifacts(
    conn: AsyncConnection,
    uid: UUID,
    run_id: str,
    manifest_row: upload_manifest.UploadManifest,
    store: ExternalBuildStore,
) -> ToolResponse | None:
    """Refresh the upload window under the per-Run lock, then reassemble each chunked artifact.

    Returns a failure (or concurrent-winner success) ``ToolResponse`` to short-circuit
    ``complete_build``, or ``None`` to proceed to validation + finalize on the now-single final
    keys (ADR-0104 §6).
    """
    ttl = timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, uid):
        refreshed = await upload_manifest.refresh_deadline(conn, "runs", uid, ttl)
    if not refreshed:
        if await upload_manifest.get_manifest(conn, "runs", uid) is None:
            return _config_error(run_id, data={"reason": "no_upload_manifest"})
        return _config_error(run_id, data={"reason": "upload_window_expired"})
    prefix = manifest_row.prefix
    try:
        for entry in manifest_row.entries:
            if entry.chunks is not None:
                await asyncio.to_thread(
                    reassemble_chunked,
                    store,
                    prefix=prefix,
                    final_key=f"{prefix}{entry.name}",
                    entry=entry,
                )
    except CategorizedError as exc:
        recorded = await _existing_build_result(conn, uid)  # a concurrent finalize won?
        if recorded is not None:
            return _complete_envelope(uid, recorded)
        return ToolResponse.failure_from_error(run_id, exc)
    return None


def _external_build_profile(run: Run) -> ExternalBuildProfile:
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ExternalBuildProfile):
        raise CategorizedError(
            "run is not an external build",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return parsed


def _created_run_guard(run: Run) -> ToolResponse | None:
    """Reject a non-CREATED Run; ``None`` means proceed to finalize."""
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
    *,
    store: ExternalBuildStore | None,
    entries: Sequence[ManifestEntry],
    prefix: str,
    chunked: bool,
) -> ToolResponse:
    """Write artifact rows, ledger result, and created -> succeeded under the per-Run lock.

    For a single-PUT manifest the upload manifest is deleted in the finalize transaction (as
    before) and ``store`` is unused (``None``). For a chunked manifest ``store`` is the live
    object store and the manifest is kept until the post-commit best-effort chunk cleanup
    finishes, so a failed cleanup leaves the manifest for the reaper to reclaim the leftover
    chunks (ADR-0104 §6).
    """
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
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = %s "
            "WHERE id = %s AND state = %s",
            (
                output.kernel_ref,
                output.debuginfo_ref or None,
                RunState.SUCCEEDED.value,
                run.id,
                RunState.CREATED.value,
            ),
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
        if not chunked:
            await upload_manifest.delete_manifest(conn, "runs", run.id)
    if chunked and store is not None:
        await _cleanup_chunks_and_manifest(conn, store, run.id, entries, prefix)
    return _complete_envelope(run.id, result)


async def _cleanup_chunks_and_manifest(
    conn: AsyncConnection,
    store: ExternalBuildStore,
    run_id: UUID,
    entries: Sequence[ManifestEntry],
    prefix: str,
) -> None:
    """Best-effort post-commit reclamation of the reassembled chunks, then the manifest.

    A failure here never fails the already-finalized build. The manifest is deleted LAST and
    only when every chunk delete succeeded, so a failed chunk delete leaves the manifest for the
    reaper to reclaim the leftover chunks (ADR-0104 §5/§6).
    """
    for entry in entries:
        if entry.chunks is None:
            continue
        for part_number in range(1, len(entry.chunks) + 1):
            key = chunk_key(prefix, entry.name, part_number)
            try:
                await asyncio.to_thread(store.delete, key)
            except CategorizedError as exc:
                _log.warning("chunk cleanup failed for %s: %s", key, exc)
                return
    try:
        await upload_manifest.delete_manifest(conn, "runs", run_id)
    except CategorizedError as exc:
        _log.warning("manifest cleanup failed for run %s: %s", run_id, exc)
