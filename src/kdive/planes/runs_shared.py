"""Shared run execution helpers used by MCP admission and worker handlers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
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


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class BuildStepResult:
    """Typed boundary for the `run_steps(step='build').result` JSON payload."""

    kernel_ref: str | None
    debuginfo_ref: str | None
    build_id: str | None
    initrd_ref: str | None = None
    cmdline: str | None = None

    @classmethod
    def load(cls, value: object) -> BuildStepResult | None:
        if not isinstance(value, Mapping):
            return None
        result = cast("Mapping[str, object]", value)
        return cls(
            kernel_ref=_optional_str(result.get("kernel_ref")),
            debuginfo_ref=_optional_str(result.get("debuginfo_ref")),
            build_id=_optional_str(result.get("build_id")),
            initrd_ref=_optional_str(result.get("initrd_ref")),
            cmdline=_optional_str(result.get("cmdline")),
        )

    def dump(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.kernel_ref is not None:
            result["kernel_ref"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            result["debuginfo_ref"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            result["initrd_ref"] = self.initrd_ref
        if self.build_id is not None:
            result["build_id"] = self.build_id
        if self.cmdline is not None:
            result["cmdline"] = self.cmdline
        return result

    def refs(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        if self.kernel_ref is not None:
            refs["kernel"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            refs["vmlinux"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            refs["initrd"] = self.initrd_ref
        return refs


async def existing_build_result(conn: AsyncConnection, run_id: UUID) -> BuildStepResult | None:
    """Return the recorded `(run_id, "build")` ledger result, or ``None``."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return BuildStepResult.load(row["result"])


async def installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The build ledger's recorded `initrd_ref`, or `None`."""
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.initrd_ref


async def finalize_build(
    conn: AsyncConnection, job: Job, run: Run, result: BuildStepResult
) -> None:
    """Record the build ledger row and drive `running -> succeeded` under the per-Run lock."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'build', 'succeeded', %s) ON CONFLICT (run_id, step) DO NOTHING",
            (run.id, Jsonb(result.dump())),
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None or RunState(row["state"]) is not RunState.RUNNING:
            return
        await conn.execute(
            "UPDATE runs SET kernel_ref = %s, debuginfo_ref = %s, state = 'succeeded' "
            "WHERE id = %s AND state = 'running'",
            (result.kernel_ref, result.debuginfo_ref, run.id),
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
    if result is not None and result.cmdline is not None and result.cmdline.strip():
        return f"{required} {result.cmdline.strip()}"
    return required


def install_method_for(system: System) -> CaptureMethod:
    """Resolve the capture method the System is provisioned for."""
    return capture_method(system.provisioning_profile)
