"""Worker handlers for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db import build_hosts
from kdive.db.build_hosts import BuildHost
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
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.build_host.dispatch import run_build_on_host
from kdive.providers.ports import Booter, InstallRequest
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.providers.runtime_paths import console_log_path, read_console_log
from kdive.security import audit
from kdive.security.artifacts.artifact_search import ArtifactSearchInputError, search_text
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.steps import (
    BuildStepResult,
    cmdline_for,
    existing_build_result,
    install_method_for,
    installed_initrd_ref,
)
from kdive.store.objectstore import (
    object_store_from_env,
    register_artifact_row,
)

_log = logging.getLogger(__name__)


async def _fail_build(conn: AsyncConnection, job: Job, run: Run, category: ErrorCategory) -> None:
    try:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
            await conn.execute(
                "UPDATE runs SET state = %s, failure_category = %s WHERE id = %s AND state = %s",
                (RunState.FAILED.value, category.value, run.id, RunState.RUNNING.value),
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
    conn: AsyncConnection, run_id: UUID, resolver: ProviderResolver
) -> ProviderRuntime:
    """Resolve the Run's provider runtime via its System's Resource kind."""
    return await resolver.runtime_for_run(conn, run_id)


async def _release_build_lease(conn: AsyncConnection, run_id: UUID) -> None:
    """Delete the run's build-host lease; called only on the SUCCESS path.

    The lease is released only when the build succeeds. On failure it is deliberately retained so
    a retry (BUILD jobs retry up to ``max_attempts``) cannot over-admit the host; the reconciler
    reclaims it when the job is terminal (see ``_build_and_record``).

    The ``conn.transaction()`` here is NOT an independent commit: the handler's earlier bare
    ``RUNS.get`` opened a long-lived implicit transaction on this non-autocommit pool connection,
    so this (and every other ``conn.transaction()`` in the handler) is a SAVEPOINT nested in that
    parent — RELEASE on exit leaves the parent open and uncommitted. The DELETE becomes durable on
    the handler's clean exit, when the pool-connection context manager commits the parent. See the
    rollback hazard documented on :func:`kdive.db.build_hosts.release_lease`.

    Errors are logged and swallowed — the reconciler is the backstop. A worker-local run holds no
    lease, so this is an idempotent no-op DELETE.
    """
    try:
        async with conn.transaction():
            await build_hosts.release_lease(conn, run_id)
    except Exception:
        _log.warning("failed to release build-host lease for run %s", run_id, exc_info=True)


async def _run_build(
    conn: AsyncConnection,
    run: Run,
    parsed: ServerBuildProfile,
    *,
    host: BuildHost,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> BuildOutput:
    """Resolve the runtime builder and run it on ``host`` through the build-host seam."""
    run_id = run.id
    builder = (await _run_runtime(conn, run_id, resolver)).builder
    return await run_build_on_host(
        builder,
        host,
        run_id,
        parsed,
        secret_registry=secret_registry,
    )


async def _resolve_build_host(
    conn: AsyncConnection, payload: BuildPayload, run_id: UUID
) -> BuildHost:
    """Resolve the BUILD payload's admitted host id to a live row.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the admitted host row has vanished
            (its lease/host disappeared between admission and build).
    """
    host_id = UUID(payload.build_host_id)
    host = await build_hosts.get_by_id(conn, host_id)
    if host is None:
        raise CategorizedError(
            "selected build host is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id), "build_host_id": str(host_id)},
        )
    return host


async def build_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> str | None:
    """Build the Run's kernel on the selected host and drive it `running -> succeeded` or failed.

    The build host is read from the BUILD payload (admitted under capacity at the ``runs.build``
    boundary): a worker-local host runs the resolved runtime builder directly; an ssh host runs a
    transport-bound remote-libvirt builder inside the materialized-identity context manager. The
    capacity lease is released on a committed path on both success and failure so a failure frees
    the slot (a worker-local run holds no lease, so the release is a harmless no-op).
    """
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
        result = await _build_and_record(
            conn, job, run, parsed, payload, resolver=resolver, secret_registry=secret_registry
        )
    await finalize_build(conn, job, run, result)
    await _release_build_lease(conn, run_id)
    return str(run_id)


async def _build_and_record(
    conn: AsyncConnection,
    job: Job,
    run: Run,
    parsed: ServerBuildProfile,
    payload: BuildPayload,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> BuildStepResult:
    """Resolve the host, run the build, and shape the ledger result; mark FAILED on error.

    The build-host lease is **not** released on the failure path. BUILD jobs retry
    (``max_attempts=3``; ``queue.fail`` requeues non-terminally), and the handler rebuilds on
    every attempt while ``existing_build_result`` is ``None``. Releasing the slot here would free
    it between attempts, letting another build grab it while attempts 2-3 still run on the host —
    ``max_concurrent`` over-admission. Instead the lease is held until the job is terminal: the
    reconciler's :func:`reclaim_orphan_build_host_leases` reclaims it (keyed on job liveness) once
    the job is dead-lettered after the last attempt. Only the success path releases the lease.
    """
    run_id = run.id
    try:
        host = await _resolve_build_host(conn, payload, run_id)
        output = await _run_build(
            conn, run, parsed, host=host, resolver=resolver, secret_registry=secret_registry
        )
    except CategorizedError as exc:
        await _fail_build(conn, job, run, exc.category)
        # LOAD-BEARING — do not remove. The handler's bare RUNS.get opened an implicit transaction
        # on this non-autocommit pool connection, so _fail_build's FAILED transition is a SAVEPOINT
        # nested in that still-open parent; the pool-connection context manager would roll the
        # parent back on the re-raise below, reverting the FAILED state. This commit makes the
        # FAILED transition durable. (The lease is intentionally NOT released here — see above.)
        await conn.commit()
        raise
    return BuildStepResult(
        kernel_ref=output.kernel_ref,
        debuginfo_ref=output.debuginfo_ref,
        build_id=output.build_id,
        cmdline=payload.cmdline,
    )


async def install_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
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
    runtime = await resolver.runtime_for_system(conn, run.system_id)
    installer = runtime.installer
    method = install_method_for(system, runtime.profile_policy)
    kernel_ref = run.kernel_ref
    cmdline = await cmdline_for(conn, run, method)
    _log.info("install: run %s resolved cmdline %r (method %s)", run_id, cmdline, method.value)
    initrd_ref = await installed_initrd_ref(conn, run_id)
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "install")
    if not claim.claimed:
        return str(run_id)
    try:
        await asyncio.to_thread(
            installer.install,
            InstallRequest(
                system_id=run.system_id,
                run_id=run_id,
                kernel_ref=kernel_ref,
                cmdline=cmdline,
                method=method,
                initrd_ref=initrd_ref,
            ),
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
    conn: AsyncConnection, system_id: UUID, secret_registry: SecretRegistry
) -> _ConsoleArtifact | None:
    try:
        redacted = await _read_redacted_console(system_id, secret_registry)
        if redacted is None:
            return None
        stored = await _store_console_artifact(system_id, redacted)
        return await _upsert_console_artifact_row(conn, system_id, stored, redacted)
    except CategorizedError as exc:
        if exc.details.get("operation") == "read_console_log":
            raise
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None
    except Exception:
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None


async def _read_redacted_console(system_id: UUID, secret_registry: SecretRegistry) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
    if not raw:
        _log.warning(
            "console log for system %s is empty or unreadable; registering no console artifact",
            system_id,
        )
        return None
    return (
        Redactor(registry=secret_registry)
        .redact_text(raw.decode("utf-8", "replace"))
        .encode("utf-8")
    )


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
    secret_registry: SecretRegistry,
) -> dict[str, Any]:
    try:
        await asyncio.to_thread(booter.boot, run.system_id)
    except CategorizedError as exc:
        artifact = None
        if (
            exc.category is ErrorCategory.READINESS_FAILURE
            and run.expected_boot_failure is not None
        ):
            artifact = await _capture_console_artifact(conn, run.system_id, secret_registry)
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
    artifact = await _capture_console_artifact(conn, run.system_id, secret_registry)
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(run.system_id),
        "boot_outcome": "ready",
        **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
    }


async def boot_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
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
    booter = (await _run_runtime(conn, run_id, resolver)).booter

    try:
        result = await _run_boot_and_capture_outcome(conn, job_ctx, run, booter, secret_registry)
    except CategorizedError:
        await _abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await _capture_console_artifact(conn, run.system_id, secret_registry)
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
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Bind the `build`/`install`/`boot` job handlers."""
    registry.register(
        JobKind.BUILD,
        lambda conn, job: build_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
    registry.register(
        JobKind.INSTALL,
        lambda conn, job: install_handler(conn, job, resolver=resolver),
    )
    registry.register(
        JobKind.BOOT,
        lambda conn, job: boot_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
