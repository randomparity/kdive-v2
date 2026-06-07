"""Worker handlers for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.idempotency import run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, Run, Sensitivity
from kdive.domain.state import IllegalTransition, RunState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import BuildPayload, RunPayload, load_payload
from kdive.planes.runs_shared import (
    cmdline_for,
    existing_build_result,
    finalize_build,
    install_method_for,
    installed_initrd_ref,
)
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.composition import ProviderRuntime, build_default_provider_runtime
from kdive.providers.ports import Booter, Builder, BuildOutput, Installer
from kdive.providers.runtime_paths import console_log_path, read_console_log
from kdive.security import audit
from kdive.security.redaction import Redactor
from kdive.store.objectstore import (
    ArtifactWriteRequest,
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


async def build_handler(conn: AsyncConnection, job: Job, builder: Builder) -> str | None:
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
        try:
            output: BuildOutput = await asyncio.to_thread(builder.build, run_id, parsed)
        except CategorizedError as exc:
            await _fail_build(conn, job, run, exc.category)
            raise
        result = {
            "kernel_ref": output.kernel_ref,
            "debuginfo_ref": output.debuginfo_ref,
            "build_id": output.build_id,
        }
        if payload.cmdline is not None:
            result["cmdline"] = payload.cmdline
    await finalize_build(conn, job, run, result)
    return str(run_id)


async def _run_step_locked(
    conn: AsyncConnection,
    run_id: UUID,
    step: str,
    fn: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
        await run_step(conn, run_id, step, fn)


async def install_handler(conn: AsyncConnection, job: Job, installer: Installer) -> str | None:
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

    async def _do() -> dict[str, Any]:
        await asyncio.to_thread(
            installer.install,
            run.system_id,
            run_id,
            kernel_ref,
            cmdline=cmdline,
            method=method,
            initrd_ref=initrd_ref,
        )
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
        return {"system_id": str(run.system_id)}

    await _run_step_locked(conn, run_id, "install", _do)
    return str(run_id)


_CONSOLE_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)

_REFRESH_CONSOLE_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


async def _existing_console_row(conn: AsyncConnection, system_id: UUID) -> _ConsoleRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONSOLE_ROW_SQL, (system_id, "%/console"))
        row = await cur.fetchone()
    return None if row is None else _ConsoleRow(row["id"], str(row["etag"]))


async def boot_handler(conn: AsyncConnection, job: Job, booter: Booter) -> str | None:
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

    async def _do() -> dict[str, Any]:
        await asyncio.to_thread(booter.boot, run.system_id)
        await audit.record(
            conn,
            job_ctx,
            audit.AuditEvent(
                tool="runs.boot",
                object_kind="runs",
                object_id=run_id,
                transition="boot",
                args={"run_id": str(run_id)},
                project=run.project,
            ),
        )
        return {"system_id": str(run.system_id)}

    try:
        await _run_step_locked(conn, run_id, "boot", _do)
    finally:
        try:
            raw = await asyncio.to_thread(read_console_log, console_log_path(run.system_id))
            if not raw:
                _log.warning(
                    "console log for system %s is empty or unreadable; "
                    "registering no console artifact",
                    run.system_id,
                )
            else:
                redacted = Redactor().redact_text(raw.decode("utf-8", "replace")).encode("utf-8")
                stored = await asyncio.to_thread(
                    lambda: object_store_from_env().put_artifact(
                        ArtifactWriteRequest(
                            tenant="local",
                            owner_kind="systems",
                            owner_id=str(run.system_id),
                            name="console",
                            data=redacted,
                            sensitivity=Sensitivity.REDACTED,
                            retention_class="console",
                        )
                    )
                )
                async with conn.transaction():
                    existing = await _existing_console_row(conn, run.system_id)
                    if existing is None:
                        await ARTIFACTS.insert(
                            conn,
                            register_artifact_row(
                                stored, owner_kind="systems", owner_id=run.system_id
                            ),
                        )
                    elif existing.etag != stored.etag:
                        await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        except Exception:
            _log.warning(
                "console artifact registration failed for system %s; boot outcome unaffected",
                run.system_id,
                exc_info=True,
            )
    return str(run_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    builder: Builder | None = None,
    installer: Installer | None = None,
    booter: Booter | None = None,
    provider_runtime: ProviderRuntime | None = None,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    runtime = provider_runtime or build_default_provider_runtime()
    build = builder or runtime.builder
    if installer is None or booter is None:
        default_installer, default_booter = runtime.install_boot()
        install: Installer = installer or default_installer
        boot: Booter = booter or default_booter
    else:
        install, boot = installer, booter

    async def _build(conn: AsyncConnection, job: Job) -> str | None:
        return await build_handler(conn, job, build)

    async def _install(conn: AsyncConnection, job: Job) -> str | None:
        return await install_handler(conn, job, install)

    async def _boot(conn: AsyncConnection, job: Job) -> str | None:
        return await boot_handler(conn, job, boot)

    registry.register(JobKind.BUILD, _build)
    registry.register(JobKind.INSTALL, _install)
    registry.register(JobKind.BOOT, _boot)
