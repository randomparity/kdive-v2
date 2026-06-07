"""Shared run execution helpers used by MCP admission and worker handlers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capture import CaptureMethod
from kdive.domain.models import Job, Run, System
from kdive.domain.state import RunState
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.profiles.provisioning import capture_method
from kdive.security import audit

_REQUIRED_BASE_CMDLINE = "console=ttyS0 root=/dev/vda"
_KDUMP_CRASHKERNEL = "crashkernel=256M"
_PLATFORM_OWNED_CMDLINE_TOKENS = ("root=", "console=", "crashkernel=")


async def existing_build_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the recorded `(run_id, "build")` ledger result, or ``None``."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None
    result = row["result"]
    return result if isinstance(result, dict) else None


async def installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The build ledger's recorded `initrd_ref`, or `None`."""
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    ref = result.get("initrd_ref")
    return ref if isinstance(ref, str) and ref else None


async def finalize_build(conn: AsyncConnection, job: Job, run: Run, result: dict[str, Any]) -> None:
    """Record the build ledger row and drive `running -> succeeded` under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result)),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None or RunState(row["state"]) is not RunState.RUNNING:
            return
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'running'",
            (result["kernel_ref"], result["debuginfo_ref"], run.id),
        )
        await audit.record(
            conn,
            job_context_from_job(job, run.project),
            audit.AuditEvent(
                tool="runs.build",
                object_kind="runs",
                object_id=run.id,
                transition="running->succeeded",
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )


def system_required_cmdline(method: CaptureMethod) -> str:
    """The platform-required kernel args for ``method``."""
    if method is CaptureMethod.KDUMP:
        return f"{_REQUIRED_BASE_CMDLINE} {_KDUMP_CRASHKERNEL}"
    return _REQUIRED_BASE_CMDLINE


def platform_owned_cmdline_token(cmdline: str | None) -> str | None:
    """Return the first platform-owned token carried by a Run debug cmdline, if present."""
    if not cmdline:
        return None
    return next((tok for tok in _PLATFORM_OWNED_CMDLINE_TOKENS if tok in cmdline), None)


async def cmdline_for(conn: AsyncConnection, run: Run, method: CaptureMethod) -> str:
    """Compose the boot cmdline: required base plus the Run's recorded debug args."""
    required = system_required_cmdline(method)
    result = await existing_build_result(conn, run.id)
    if result is not None:
        value = result.get("cmdline")
        if isinstance(value, str) and value.strip():
            return f"{required} {value.strip()}"
    return required


def install_method_for(system: System) -> CaptureMethod:
    """Resolve the capture method the System is provisioned for."""
    return capture_method(system.provisioning_profile)
