"""Worker handlers for the `vmcore.*` retrieve plane."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.artifact_queries import raw_vmcore_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Job, JobKind, System
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import CaptureVmcorePayload, load_payload
from kdive.providers.ports import Retriever
from kdive.providers.resolver import ProviderResolver
from kdive.security import audit
from kdive.store.objectstore import register_artifact_row


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
        existing = await raw_vmcore_key(conn, system_id)
        if existing is not None:
            ensure_method_match(existing, method, system_id)
            return existing
        return system


async def finalize_capture(
    conn: AsyncConnection, job: Job, system: System, method: CaptureMethod, output: Any
) -> str:
    """Insert both artifact rows + audit under the per-System lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        existing = await raw_vmcore_key(conn, system.id)
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


async def capture_handler(
    conn: AsyncConnection,
    job: Job,
    retriever: Retriever | None = None,
    *,
    resolver: ProviderResolver | None = None,
) -> str | None:
    """Capture the System's vmcore and store the raw + redacted rows."""
    payload = load_payload(job, CaptureVmcorePayload)
    system_id = UUID(payload.system_id)
    method = payload.method
    precheck = await precheck_system(conn, system_id, method)
    if isinstance(precheck, str):
        return precheck
    if retriever is None:
        if resolver is None:
            raise RuntimeError("vmcore handlers require a resolver or an explicit retriever")
        retriever = (await resolver.runtime_for_system(conn, system_id)).retriever
    output = await asyncio.to_thread(retriever.capture, system_id, method)
    return await finalize_capture(conn, job, precheck, method, output)


def register_handlers(
    registry: HandlerRegistry,
    *,
    retriever: Retriever | None = None,
    resolver: ProviderResolver | None = None,
) -> None:
    """Bind the `capture_vmcore` job handler."""
    if retriever is None and resolver is None:
        raise RuntimeError("vmcore handlers require a resolver or an explicit retriever")

    registry.register(
        JobKind.CAPTURE_VMCORE,
        lambda conn, job: capture_handler(conn, job, retriever, resolver=resolver),
    )
