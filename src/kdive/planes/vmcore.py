"""Worker handlers for the `vmcore.*` retrieve plane."""

from __future__ import annotations

import asyncio
from typing import Any, LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureVmcorePayload, load_payload
from kdive.mcp.job_context import context_from_job as job_context_from_job
from kdive.providers.composition import ProviderRuntime, retriever_from_env
from kdive.providers.ports import Retriever
from kdive.security import audit
from kdive.store.objectstore import register_artifact_row

RAW_KEY_SQL: LiteralString = (
    "SELECT object_key FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s "
    "AND object_key LIKE %s AND object_key NOT LIKE %s"
)
RAW_KEY_LIKE = "%/vmcore-%"
REDACTED_LIKE = "%-redacted"


async def existing_raw_key(conn: AsyncConnection, system_id: UUID) -> str | None:
    """Return the System's raw `vmcore-{method}` object key, or ``None``."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(RAW_KEY_SQL, (system_id, RAW_KEY_LIKE, REDACTED_LIKE))
        row = await cur.fetchone()
    return None if row is None else str(row["object_key"])


def captured_method(object_key: str) -> str:
    """The method suffix of a raw vmcore key (`.../vmcore-host_dump` -> `host_dump`)."""
    _, sep, method = object_key.rpartition("/vmcore-")
    if not sep or not method:
        raise CategorizedError(
            "malformed raw vmcore object key (no method suffix)",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"object_key": object_key},
        )
    return method


def ensure_method_match(existing_key: str, method: CaptureMethod, system_id: UUID) -> None:
    """Raise `configuration_error` when an existing core used another capture method."""
    captured = captured_method(existing_key)
    if captured != method.value:
        raise CategorizedError(
            "a vmcore captured via a different method already exists for this System",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "system_id": str(system_id),
                "existing_method": captured,
                "requested_method": method.value,
            },
        )


async def precheck_system(
    conn: AsyncConnection, system_id: UUID, method: CaptureMethod
) -> System | str:
    """Under the per-System lock, return an existing same-method key, or the System to capture."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                "capture target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        existing = await existing_raw_key(conn, system_id)
        if existing is not None:
            ensure_method_match(existing, method, system_id)
            return existing
        return system


async def finalize_capture(
    conn: AsyncConnection, job: Job, system: System, method: CaptureMethod, output: Any
) -> str:
    """Insert both artifact rows + audit under the per-System lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        existing = await existing_raw_key(conn, system.id)
        if existing is not None:
            ensure_method_match(existing, method, system.id)
            return existing
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.raw, owner_kind="systems", owner_id=system.id)
        )
        await ARTIFACTS.insert(
            conn, register_artifact_row(output.redacted, owner_kind="systems", owner_id=system.id)
        )
        await audit.record(
            conn,
            job_context_from_job(job, system.project),
            audit.AuditEvent(
                tool="vmcore.fetch",
                object_kind="systems",
                object_id=system.id,
                transition="capture_vmcore",
                args={"system_id": str(system.id)},
                project=system.project,
            ),
        )
    return str(output.raw.key)


async def capture_handler(conn: AsyncConnection, job: Job, retriever: Retriever) -> str | None:
    """Capture the System's vmcore and store the raw + redacted rows."""
    payload = load_payload(job, CaptureVmcorePayload)
    system_id = UUID(payload.system_id)
    method = CaptureMethod(payload.method)
    precheck = await precheck_system(conn, system_id, method)
    if isinstance(precheck, str):
        return precheck
    output = await asyncio.to_thread(retriever.capture, system_id, method)
    return await finalize_capture(conn, job, precheck, method, output)


def register_handlers(
    registry: HandlerRegistry,
    *,
    retriever: Retriever | None = None,
    provider_runtime: ProviderRuntime | None = None,
) -> None:
    """Bind the `capture_vmcore` job handler; build the retriever lazily from env."""
    active = retriever or (
        provider_runtime.retriever() if provider_runtime else retriever_from_env()
    )

    async def _capture(conn: AsyncConnection, job: Job) -> str | None:
        return await capture_handler(conn, job, active)

    registry.register(JobKind.CAPTURE_VMCORE, _capture)
