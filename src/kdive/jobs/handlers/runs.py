"""Worker handlers for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.idempotency import abandon_run_step, claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, Run, Sensitivity
from kdive.domain.state import IllegalTransition, RunState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs_shared import finalize_build
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import BuildPayload, RunPayload, load_payload
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.ports import Booter, Builder, BuildOutput, Installer
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.providers.runtime_paths import console_log_path, read_console_log
from kdive.security import audit
from kdive.security.artifacts.artifact_search import ArtifactSearchInputError, search_text
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.redaction import Redactor
from kdive.services.runs.steps import (
    BuildStepResult,
    cmdline_for,
    existing_build_result,
    install_method_for,
    installed_initrd_ref,
)
from kdive.store.objectstore import (
    ArtifactWriteRequest,
    StoredArtifact,
    object_store_from_env,
    register_artifact_row,
)

_log = logging.getLogger(__name__)


async def _fail_build(conn: AsyncConnection, job: Job, run: Run, category: ErrorCategory) -> None:
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            await conn.execute(
                "UPDATE runs SET state = 'failed', failure_category = %s "
                "WHERE id = %s AND state = 'running'",
                (category.value, run.id),
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT state FROM runs WHERE id = %s", (run.id,))
                row = await cur.fetchone()
            if row is None or RunState(row["state"]) is not RunState.FAILED:
                raise IllegalTransition(f"run {run.id} was not running at build failure")
            await audit.record(
                conn,
                job_context_from_job(job, run.project),
                audit.AuditEvent(
                    tool="runs.build",
                    object_kind="runs",
                    object_id=run.id,
                    transition="running->failed",
                    args={"run_id": str(run.id)},
                    project=run.project,
                ),
            )
    except IllegalTransition:
        _log.warning(
            "build of run %s failed (%s) but it is already terminal; failure not recorded "
            "on the Run (a concurrent cancel won)",
            run.id,
            category.value,
        )


async def _abandon_run_step_best_effort(conn: AsyncConnection, run_id: UUID, step: str) -> None:
    try:
        await abandon_run_step(conn, run_id, step)
    except Exception:
        _log.warning(
            "failed to abandon %s step claim for run %s; preserving original failure",
            step,
            run_id,
            exc_info=True,
        )


async def _run_runtime(
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver | None
) -> ProviderRuntime:
    """Resolve the Run's provider runtime via its System's Resource kind."""
    if resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")
    return await resolver.runtime_for_run(conn, run_id)


async def build_handler(
    conn: AsyncConnection,
    job: Job,
    builder: Builder | None = None,
    *,
    resolver: ProviderResolver | None = None,
) -> str | None:
    """Build the Run's kernel and drive it `running -> succeeded` or failed."""
    payload = load_payload(job, BuildPayload)
    run_id = UUID(payload.run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "build target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    parsed = BuildProfile.parse(run.build_profile)
    if not isinstance(parsed, ServerBuildProfile):
        raise CategorizedError(
            "external-source run reached the server build handler",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    result = await existing_build_result(conn, run_id)
    if result is None:
        if builder is None:
            builder = (await _run_runtime(conn, run_id, resolver)).builder
        try:
            output: BuildOutput = await asyncio.to_thread(builder.build, run_id, parsed)
        except CategorizedError as exc:
            await _fail_build(conn, job, run, exc.category)
            raise
        result = BuildStepResult(
            kernel_ref=output.kernel_ref,
            debuginfo_ref=output.debuginfo_ref,
            build_id=output.build_id,
            cmdline=payload.cmdline,
        )
    await finalize_build(conn, job, run, result)
    return str(run_id)


async def install_handler(
    conn: AsyncConnection,
    job: Job,
    installer: Installer | None = None,
    *,
    resolver: ProviderResolver | None = None,
) -> str | None:
    """Stage the built kernel for direct-kernel boot, recording the `install` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None or run.kernel_ref is None:
        raise CategorizedError(
            "install target run is gone or unbuilt (no kernel_ref)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    system = await SYSTEMS.get(conn, run.system_id)
    if system is None:
        raise CategorizedError(
            "install target system is gone",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id), "system_id": str(run.system_id)},
        )
    method = install_method_for(system)
    kernel_ref = run.kernel_ref
    cmdline = await cmdline_for(conn, run, method)
    _log.info("install: run %s resolved cmdline %r (method %s)", run_id, cmdline, method.value)
    initrd_ref = await installed_initrd_ref(conn, run_id)
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "install")
    if not claim.claimed:
        return str(run_id)
    if installer is None:
        if resolver is None:
            raise RuntimeError("runs handlers require a resolver or explicit run ports")
        installer = (await resolver.runtime_for_system(conn, run.system_id)).installer
    try:
        await asyncio.to_thread(
            installer.install,
            run.system_id,
            run_id,
            kernel_ref,
            cmdline=cmdline,
            method=method,
            initrd_ref=initrd_ref,
        )
    except Exception:
        await _abandon_run_step_best_effort(conn, run_id, "install")
        raise
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        await complete_run_step(conn, run_id, "install", {"system_id": str(run.system_id)})
        await audit.record(
            conn,
            job_ctx,
            audit.AuditEvent(
                tool="runs.install",
                object_kind="runs",
                object_id=run_id,
                transition="install",
                args={"run_id": str(run_id)},
                project=run.project,
            ),
        )
    return str(run_id)


_CONSOLE_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)

_REFRESH_CONSOLE_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


class _ConsoleArtifact(NamedTuple):
    id: UUID
    object_key: str
    data: bytes


async def _existing_console_row(conn: AsyncConnection, system_id: UUID) -> _ConsoleRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONSOLE_ROW_SQL, (system_id, "%/console"))
        row = await cur.fetchone()
    return None if row is None else _ConsoleRow(row["id"], str(row["etag"]))


async def _capture_console_artifact(
    conn: AsyncConnection, system_id: UUID
) -> _ConsoleArtifact | None:
    try:
        redacted = await _read_redacted_console(system_id)
        if redacted is None:
            return None
        stored = await _store_console_artifact(system_id, redacted)
        return await _upsert_console_artifact_row(conn, system_id, stored, redacted)
    except Exception:
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None


async def _read_redacted_console(system_id: UUID) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
    if not raw:
        _log.warning(
            "console log for system %s is empty or unreadable; registering no console artifact",
            system_id,
        )
        return None
    return Redactor().redact_text(raw.decode("utf-8", "replace")).encode("utf-8")


async def _store_console_artifact(system_id: UUID, redacted: bytes) -> StoredArtifact:
    def _put() -> StoredArtifact:
        return object_store_from_env().put_artifact(
            ArtifactWriteRequest(
                tenant="local",
                owner_kind="systems",
                owner_id=str(system_id),
                name="console",
                data=redacted,
                sensitivity=Sensitivity.REDACTED,
                retention_class="console",
            )
        )

    return await asyncio.to_thread(_put)


async def _upsert_console_artifact_row(
    conn: AsyncConnection,
    system_id: UUID,
    stored: StoredArtifact,
    redacted: bytes,
) -> _ConsoleArtifact:
    async with conn.transaction():
        existing = await _existing_console_row(conn, system_id)
        if existing is None:
            inserted = await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="systems", owner_id=system_id)
            )
            return _ConsoleArtifact(inserted.id, inserted.object_key, redacted)
        if existing.etag != stored.etag:
            await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        return _ConsoleArtifact(existing.id, stored.key, redacted)


def _expected_crash_matches(run: Run, redacted_console: bytes) -> bool:
    expected = run.expected_boot_failure
    if expected is None or expected.get("kind") != "console_crash":
        return False
    pattern = expected.get("pattern")
    if not isinstance(pattern, str):
        return False
    try:
        return (
            search_text(
                redacted_console,
                pattern=pattern,
                before_lines=0,
                after_lines=0,
                max_matches=1,
            ).match_count
            > 0
        )
    except ArtifactSearchInputError:
        return False


async def _record_boot_audit(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
) -> None:
    await audit.record(
        conn,
        job_ctx,
        audit.AuditEvent(
            tool="runs.boot",
            object_kind="runs",
            object_id=run.id,
            transition="boot",
            args={"run_id": str(run.id)},
            project=run.project,
        ),
    )


async def _run_boot_and_capture_outcome(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    booter: Booter,
) -> dict[str, Any]:
    try:
        await asyncio.to_thread(booter.boot, run.system_id)
    except CategorizedError as exc:
        artifact = None
        if (
            exc.category is ErrorCategory.READINESS_FAILURE
            and run.expected_boot_failure is not None
        ):
            artifact = await _capture_console_artifact(conn, run.system_id)
        if artifact is not None and artifact.data and _expected_crash_matches(run, artifact.data):
            await _record_boot_audit(conn, job_ctx, run)
            return {
                "system_id": str(run.system_id),
                "boot_outcome": "expected_crash_observed",
                "expectation_matched": True,
                "evidence_kind": "console",
                "evidence_artifact_id": str(artifact.id),
            }
        raise
    artifact = await _capture_console_artifact(conn, run.system_id)
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(run.system_id),
        "boot_outcome": "ready",
        **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
    }


async def boot_handler(
    conn: AsyncConnection,
    job: Job,
    booter: Booter | None = None,
    *,
    resolver: ProviderResolver | None = None,
) -> str | None:
    """Boot the installed kernel and confirm run-readiness, recording the `boot` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "boot target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "boot")
    if not claim.claimed:
        return str(run_id)
    if booter is None:
        booter = (await _run_runtime(conn, run_id, resolver)).booter

    try:
        result = await _run_boot_and_capture_outcome(conn, job_ctx, run, booter)
    except CategorizedError:
        await _abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await _capture_console_artifact(conn, run.system_id)
        finally:
            raise
    except Exception:
        await _abandon_run_step_best_effort(conn, run_id, "boot")
        raise
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, run.system_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        await complete_run_step(conn, run_id, "boot", result)
    return str(run_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    builder: Builder | None = None,
    installer: Installer | None = None,
    booter: Booter | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    if builder is None and installer is None and booter is None and resolver is None:
        raise RuntimeError("runs handlers require a resolver or explicit run ports")

    registry.register(
        JobKind.BUILD, lambda conn, job: build_handler(conn, job, builder, resolver=resolver)
    )
    registry.register(
        JobKind.INSTALL, lambda conn, job: install_handler(conn, job, installer, resolver=resolver)
    )
    registry.register(
        JobKind.BOOT, lambda conn, job: boot_handler(conn, job, booter, resolver=resolver)
    )
