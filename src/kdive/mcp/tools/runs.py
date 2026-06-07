"""The `runs.*` MCP tools — the Run join-point (ADR-0026).

`runs.create` binds a Run to a `ready` System (whose Allocation must be `active`, fixing
the Run's Allocation per the binding invariant) and an Investigation, and flips the
Investigation `open -> active` on its first Run — all in one transaction holding a
per-System then per-Investigation advisory lock (the global ALLOCATION→SYSTEM→
INVESTIGATION→RUN order). `runs.get` renders a Run; a `failed` Run maps to a failure
envelope carrying the Run's own `failure_category`. RBAC: `create` requires `operator`;
`get` requires project membership. Authz denials raise (ADR-0020: no authz ErrorCategory).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol
from uuid import UUID, uuid4

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, ARTIFACTS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Investigation, Job, JobKind, ResourceKind, Run, Sensitivity, System
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.jobs import queue
from kdive.jobs.payloads import BuildPayload
from kdive.log import bind_context
from kdive.mcp.auth import RequestContext, current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    authorizing as job_authorizing,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    context_from_job as job_context_from_job,
)
from kdive.mcp.tools._common import (
    job_envelope,
)
from kdive.mcp.tools._common import (
    stale_handle as _stale_handle,
)
from kdive.profiles.build import BuildProfile, ExternalBuildProfile
from kdive.providers.composition import (
    console_log_path as _console_log_path,
)
from kdive.providers.composition import (
    read_console_log as _read_console_log,
)
from kdive.providers.composition import (
    validate_external_artifacts,
)
from kdive.providers.ports import BuildOutput, ValidatedUpload
from kdive.security import audit
from kdive.security.rbac import Role, require_role
from kdive.store.objectstore import (
    HeadResult,
    StoredArtifact,
    object_store_from_env,
    register_artifact_row,
)

_log = logging.getLogger(__name__)

_RUN_HOSTABLE = frozenset({SystemState.READY})
_SYSTEM_GONE = frozenset({SystemState.TORN_DOWN, SystemState.FAILED, SystemState.CRASHED})
_ALLOC_HOSTABLE = frozenset({AllocationState.ACTIVE})
_INVESTIGATION_OPEN_FOR_RUN = frozenset({InvestigationState.OPEN, InvestigationState.ACTIVE})


def console_log_path(system_id: UUID) -> Path:
    return _console_log_path(system_id)


def read_console_log(path: Path) -> bytes:
    return _read_console_log(path)


def _envelope_for_run(run: Run, *, required_cmdline: str | None = None) -> ToolResponse:
    """Render a Run; `failed` becomes a failure envelope carrying its `failure_category`.

    ``required_cmdline`` (the platform args the boot always injects, ADR-0061) is advertised in
    the success envelope so an agent appends its debug args to ``runs.build`` without clobbering
    ``root=``/``console=``.
    """
    if run.state is RunState.FAILED:
        category = run.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(str(run.id), category, data={"current_status": run.state.value})
    if run.state in (RunState.CREATED, RunState.RUNNING):
        actions = ["runs.get", "runs.build"]
    else:
        actions = ["runs.get"]
    data = {"project": run.project}
    if required_cmdline is not None:
        data["required_cmdline"] = required_cmdline
    return ToolResponse.success(
        str(run.id), run.state.value, suggested_next_actions=actions, data=data
    )


async def get_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Return a Run the caller's project owns, advertising the boot's required cmdline."""
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            system = await SYSTEMS.get(conn, run.system_id)
        required = (
            system_required_cmdline(_install_method_for(system)) if system is not None else None
        )
        return _envelope_for_run(run, required_cmdline=required)


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv_uid: UUID,
    sys_uid: UUID,
    build_profile: dict[str, Any],
    *,
    project: str,
) -> ToolResponse:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, sys_uid),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, inv_uid),
    ):
        system = await SYSTEMS.get(conn, sys_uid)
        if system is None:
            return _config_error(str(sys_uid))
        if system.state in _SYSTEM_GONE:
            return _stale_handle(str(sys_uid), current_status=system.state.value)
        if system.state not in _RUN_HOSTABLE:
            return _config_error(str(sys_uid), data={"current_status": system.state.value})
        inv = await _investigation_for_update(conn, inv_uid)
        if inv is None:
            return _config_error(str(inv_uid))
        if inv.state not in _INVESTIGATION_OPEN_FOR_RUN:
            return _config_error(str(inv_uid), data={"current_status": inv.state.value})
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=now,
                updated_at=now,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=project,
                investigation_id=inv_uid,
                system_id=sys_uid,
                state=RunState.CREATED,
                build_profile=build_profile,
            ),
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool="runs.create",
                object_kind="runs",
                object_id=run.id,
                transition="->created",
                args={"investigation_id": str(inv_uid), "system_id": str(sys_uid)},
                project=project,
            ),
        )
        if inv.state is InvestigationState.OPEN:
            await INVESTIGATIONS.update_state(conn, inv_uid, InvestigationState.ACTIVE)
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool="runs.create",
                    object_kind="investigations",
                    object_id=inv_uid,
                    transition="open->active",
                    args={"investigation_id": str(inv_uid)},
                    project=project,
                ),
            )
        await conn.execute(
            "UPDATE investigations SET last_run_at = now() WHERE id = %s", (inv_uid,)
        )
    return ToolResponse.success(
        str(run.id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data={
            "project": project,
            "investigation_id": str(inv_uid),
            "system_id": str(sys_uid),
        },
    )


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    investigation_id: str,
    system_id: str,
    build_profile: dict[str, Any],
) -> ToolResponse:
    """Bind a Run to a `ready` System + an Investigation; flip `open -> active` on the first Run."""
    inv_uid = _as_uuid(investigation_id)
    if inv_uid is None:
        return _config_error(investigation_id)
    sys_uid = _as_uuid(system_id)
    if sys_uid is None:
        return _config_error(system_id)
    if not isinstance(build_profile, dict):
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            inv = await INVESTIGATIONS.get(conn, inv_uid)
            if inv is None or inv.project not in ctx.projects:
                return _config_error(investigation_id)
            require_role(ctx, inv.project, Role.OPERATOR)
            system = await SYSTEMS.get(conn, sys_uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            if system.project != inv.project:
                return _config_error(system_id)
            alloc = await ALLOCATIONS.get(conn, system.allocation_id)
            if alloc is None or alloc.state not in _ALLOC_HOSTABLE:
                current = alloc.state.value if alloc is not None else "missing"
                return _stale_handle(system_id, current_status=current)
            return await _create_locked(
                conn, ctx, inv_uid, sys_uid, build_profile, project=inv.project
            )


_RUN_BUILD_TERMINAL = frozenset({RunState.FAILED, RunState.CANCELED})


def _run_job_envelope(job: Job, run_id: UUID) -> ToolResponse:
    return job_envelope(job, "run_id", run_id)


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


async def _build_locked(
    conn: AsyncConnection, ctx: RequestContext, run: Run, cmdline: str | None
) -> ToolResponse:
    """Admit the build under the per-Run lock: flip `created → running`, then enqueue."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM runs WHERE id = %s FOR UPDATE", (run.id,))
            row = await cur.fetchone()
        if row is None:
            return _config_error(str(run.id))
        state = RunState(row["state"])
        if state in _RUN_BUILD_TERMINAL:
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
    return _run_job_envelope(job, run.id)


async def build_run(
    pool: AsyncConnectionPool, ctx: RequestContext, run_id: str, *, cmdline: str | None = None
) -> ToolResponse:
    """Admit an idempotent build for a Run: drive `created → running` and enqueue the job.

    ``cmdline`` is the Run's **debug args** — recorded in the build ledger and appended to the
    platform-required base at boot (ADR-0061). It must not carry a platform-owned token
    (``root=``/``console=``/``crashkernel=``): those are injected, and a duplicate would override
    the base under the kernel's last-occurrence rule, so such a cmdline is rejected here. It binds
    on the first build enqueue for a Run (dedup key ``{run_id}:build``, ``ON CONFLICT DO NOTHING``).
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    owned = _platform_owned_token(cmdline)
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


async def _existing_build_result(conn: AsyncConnection, run_id: UUID) -> dict[str, Any] | None:
    """Return the recorded `(run_id, "build")` ledger result, or ``None`` (short read)."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None
    result = row["result"]
    return result if isinstance(result, dict) else None


async def _installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The build ledger's recorded `initrd_ref`, or `None` (server builds record none).

    The external-build lane records the uploaded initrd's object key in the `(run_id, "build")`
    ledger result (`_finalize_external_build`); a blank/absent value means no external initrd is
    staged (a bzImage with an embedded initramfs), so the install emits no `<initrd>`.
    """
    result = await _existing_build_result(conn, run_id)
    if result is None:
        return None
    ref = result.get("initrd_ref")
    return ref if isinstance(ref, str) and ref else None


async def _finalize_build(
    conn: AsyncConnection, job: Job, run: Run, result: dict[str, Any]
) -> None:
    """Record the build ledger row and drive `running → succeeded` under the per-Run lock."""
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
            return  # already finalized (succeeded) or superseded (canceled) — no-op
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


class _CompleteBuildValidator(Protocol):
    def validate(
        self,
        run_id: UUID,
        manifest: Sequence[ManifestEntry],
        keys: Mapping[str, str],
        declared_build_id: str | None,
    ) -> ValidatedUpload: ...


class _StoreBackedValidator:
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
    validator: _CompleteBuildValidator | None = None,
) -> ToolResponse:
    """Validate an external Run's uploads and finalize it ``created → succeeded``.

    Idempotent: a recorded ``(run_id, "build")`` ledger row short-circuits to the prior
    success BEFORE the CREATED/source guard, so a retry after a dropped connection returns
    success, not an illegal-transition error. Requires operator.

    ``cmdline`` is the Run's debug args, persisted in the build ledger and appended to the
    platform-required base at boot (ADR-0061) — the same composition the server lane uses. It
    must not carry a platform-owned token (``root=``/``console=``/``crashkernel=``); such a
    cmdline is rejected here, mirroring ``build_run``.
    """
    validator = validator or _StoreBackedValidator()
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    owned = _platform_owned_token(cmdline)
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
    """Build the success envelope from a ledger ``result`` (used live and on replay)."""
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
    """Write artifact rows + ledger + drive created→succeeded under the per-Run lock.

    Collapses ``created → succeeded`` in one locked transaction via guarded raw ``UPDATE``s
    (``WHERE state='created'``), bypassing ``can_transition`` the same way the server lane's
    ``_finalize_build`` does for ``running → succeeded`` — so no ``state.py`` edge change is
    needed. One write-once ``artifacts`` row is written per uploaded object, keyed by its
    OWN object key (``keys[name]``) — so an ``initrd`` is recorded against its real key,
    never the kernel's or vmlinux's (``BuildOutput`` carries no initrd field).
    """
    # ``cmdline`` is recorded in the LEDGER result, not in build_profile (an immutable
    # request input; ExternalBuildProfile is extra="forbid"). ``_cmdline_for`` reads this
    # ledger result and applies it at boot, so the external lane's cmdline is live (ADR-0056).
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
        if state is RunState.SUCCEEDED:  # a racing complete won; idempotent success
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


# --- install + boot plane (#19, ADR-0030) --------------------------------------------

# The platform-required kernel args the install/boot plane always injects: the serial console
# the readiness/crash classifier tails, and the root device provisioning attaches as vda. The
# kdump tier additionally reserves `crashkernel=`. The Run's recorded cmdline is the agent's
# debug args, appended AFTER these so a boot can never lose `root=`/`console=` (ADR-0061
# supersedes the ADR-0056 replace semantics; the device tracks provisioning's `target dev`).
_REQUIRED_BASE_CMDLINE = "console=ttyS0 root=/dev/vda"
_KDUMP_CRASHKERNEL = "crashkernel=256M"
# Tokens the platform owns and injects (ADR-0061); a Run's debug cmdline must not carry them,
# because a duplicate would win under the kernel's last-occurrence rule and override the base.
_PLATFORM_OWNED_CMDLINE_TOKENS = ("root=", "console=", "crashkernel=")


def _platform_owned_token(cmdline: str | None) -> str | None:
    """The first platform-owned token a Run cmdline carries (it may not), else ``None``."""
    if not cmdline:
        return None
    return next((tok for tok in _PLATFORM_OWNED_CMDLINE_TOKENS if tok in cmdline), None)


def system_required_cmdline(method: CaptureMethod) -> str:
    """The platform-required kernel args for ``method`` (advertised on the Run and always
    injected at boot). kdump adds the `crashkernel=` reservation; the other tiers do not."""
    if method is CaptureMethod.KDUMP:
        return f"{_REQUIRED_BASE_CMDLINE} {_KDUMP_CRASHKERNEL}"
    return _REQUIRED_BASE_CMDLINE


async def _cmdline_for(conn: AsyncConnection, run: Run, method: CaptureMethod) -> str:
    """Compose the boot cmdline: the required base, then the Run's debug args (ADR-0061).

    The debug portion is the `(run_id, "build")` ledger `result["cmdline"]`, written by the
    build handler (server lane, `runs.build cmdline=`) or `complete_build` (external lane). It
    is appended after :func:`system_required_cmdline`, so the platform-required `root=`/`console`
    (and the kdump `crashkernel=`) are always present regardless of agent input.
    """
    required = system_required_cmdline(method)
    result = await _existing_build_result(conn, run.id)
    if result is not None:
        value = result.get("cmdline")
        if isinstance(value, str) and value.strip():
            return f"{required} {value.strip()}"
    return required


def _local_libvirt_section(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """The `provider['local-libvirt']` section of a stored profile, or `{}` (loose read).

    Navigates the persisted **alias** key (`ResourceKind.LOCAL_LIBVIRT.value`, `"local-libvirt"`),
    which is what `ProvisioningProfile.model_dump(by_alias=True)` writes — not the Python
    attribute spelling `local_libvirt`. A missing/odd-shaped profile yields `{}` rather than
    raising, mirroring `_cmdline_for`'s loose read (ADR-0051 Decision 1).
    """
    provider = profile.get("provider")
    if not isinstance(provider, Mapping):
        return {}
    section = provider.get(ResourceKind.LOCAL_LIBVIRT.value)
    return section if isinstance(section, Mapping) else {}


def _install_method_for(system: System) -> CaptureMethod:
    """Resolve the capture method the System is provisioned for (ADR-0051 Decision 1).

    A non-empty `crashkernel` reservation means the System is provisioned for kdump
    (`crashkernel ⇔ kdump`, ADR-0049 §5); otherwise the `debug` flags select the non-kdump
    method, defaulting to the always-on `console` baseline (ADR-0049 §4).
    """
    section = _local_libvirt_section(system.provisioning_profile)
    crashkernel = section.get("crashkernel")
    if isinstance(crashkernel, str) and crashkernel.strip():
        return CaptureMethod.KDUMP
    debug = section.get("debug")
    debug = debug if isinstance(debug, Mapping) else {}
    if debug.get("gdbstub") is True:
        return CaptureMethod.GDBSTUB
    if debug.get("preserve_on_crash") is True:
        return CaptureMethod.HOST_DUMP
    return CaptureMethod.CONSOLE


async def install_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent install for a built, SUCCEEDED Run.

    The boot cmdline is composed at boot from the System's capture method (ADR-0051 §1) plus the
    Run's debug args (ADR-0061), with the platform injecting `crashkernel=` for a kdump System —
    so there is no agent-supplied cmdline token to validate here.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            return await _enqueue_step(conn, ctx, run, JobKind.INSTALL, "install", "runs.install")


async def boot_run(pool: AsyncConnectionPool, ctx: RequestContext, run_id: str) -> ToolResponse:
    """Admit an idempotent boot for a built, installed Run (requires a succeeded install step).

    The install gate checks the **recorded** (finalized) install step, so a caller must
    ``jobs.wait`` on the install job before `runs.boot`; firing boot while the install job is
    still queued returns `configuration_error` "install first" (the install row is not yet
    committed). After the install job succeeds the gate passes.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        return _config_error(run_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            run = await RUNS.get(conn, uid)
            if run is None or run.project not in ctx.projects:
                return _config_error(run_id)
            require_role(ctx, run.project, Role.OPERATOR)
            if run.state is not RunState.SUCCEEDED:
                return _config_error(run_id, data={"current_status": run.state.value})
            if not await _has_succeeded_step(conn, uid, "install"):
                return _config_error(run_id, data={"reason": "install_first"})
            return await _enqueue_step(conn, ctx, run, JobKind.BOOT, "boot", "runs.boot")


async def _has_succeeded_step(conn: AsyncConnection, run_id: UUID, step: str) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM run_steps WHERE run_id = %s AND step = %s AND state = 'succeeded'",
            (run_id, step),
        )
        return await cur.fetchone() is not None


async def _enqueue_step(
    conn: AsyncConnection,
    ctx: RequestContext,
    run: Run,
    kind: JobKind,
    step: str,
    tool: str,
) -> ToolResponse:
    """Enqueue an install/boot step job under the per-Run lock; the Run state is untouched."""
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run.id):
        job = await queue.enqueue(
            conn,
            kind,
            {"run_id": str(run.id)},
            job_authorizing(ctx, run.project),
            f"{run.id}:{step}",
        )
        await audit.record(
            conn,
            ctx,
            audit.AuditEvent(
                tool=tool,
                object_kind="runs",
                object_id=run.id,
                transition=step,
                args={"run_id": str(run.id)},
                project=run.project,
            ),
        )
    return _run_job_envelope(job, run.id)


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""

    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Render a Run; a failed Run maps to a failure envelope. Requires project membership."""
        return await get_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        system_id: Annotated[str, Field(description="Ready System (active Allocation) to bind.")],
        build_profile: Annotated[
            dict[str, Any], Field(description="Build profile for the Run's kernel.")
        ],
    ) -> ToolResponse:
        """Bind a Run to a ready System and Investigation in one transaction. Requires operator."""
        return await create_run(
            pool,
            current_context(),
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=build_profile,
        )

    @app.tool(
        name="runs.build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_build(
        run_id: Annotated[str, Field(description="The Run to build.")],
        cmdline: Annotated[
            str | None,
            Field(
                description="Kernel command line recorded in the build ledger and applied at "
                "boot (e.g. 'console=ttyS0 dhash_entries=1'). Omit for the method default. "
                "Bound on the first build of a Run."
            ),
        ] = None,
    ) -> ToolResponse:
        """Enqueue the kernel build job for a Run; poll jobs.* for completion. Requires operator."""
        return await build_run(pool, current_context(), run_id, cmdline=cmdline)

    @app.tool(
        name="runs.complete_build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_complete_build(
        run_id: Annotated[str, Field(description="The external-build Run to finalize.")],
        cmdline: Annotated[
            str,
            Field(
                description="Kernel command line (e.g. 'console=ttyS0 dhash_entries=1'). "
                "Recorded in the build ledger and applied at boot via runs.install/runs.boot "
                "(ADR-0056)."
            ),
        ],
        build_id: Annotated[
            str | None,
            Field(
                description="GNU build-id as hex (e.g. from `readelf -n vmlinux`); required iff "
                "a vmlinux was uploaded. Case-insensitive."
            ),
        ] = None,
    ) -> ToolResponse:
        """Validate an external Run's uploads and finalize it to succeeded. Operator only."""
        return await complete_build(
            pool, current_context(), run_id, build_id=build_id, cmdline=cmdline
        )

    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
    ) -> ToolResponse:
        """Enqueue the install job for a built Run; poll jobs.* for completion. Operator only."""
        return await install_run(pool, current_context(), run_id)

    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
    ) -> ToolResponse:
        """Enqueue the boot job for an installed Run; poll jobs.* for completion. Operator only."""
        return await boot_run(pool, current_context(), run_id)
