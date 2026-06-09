"""`runs.create` MCP handler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, ExpectedBootFailure, Investigation, Run, System
from kdive.domain.profile_documents import SerializedExpectedBootFailure
from kdive.domain.state import InvestigationState, RunState
from kdive.domain.system_reuse import ReuseRequirement, read_system_sizing, snapshot_satisfies
from kdive.log import bind_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import config_error as _config_error
from kdive.mcp.tools._common import stale_handle as _stale_handle
from kdive.mcp.tools.lifecycle.runs.common import (
    ALLOC_HOSTABLE,
    INVESTIGATION_OPEN_FOR_RUN,
    RUN_HOSTABLE,
    RUN_NON_TERMINAL,
    SYSTEM_GONE,
)
from kdive.profiles.build import BuildProfile, ParsedBuildProfile, dump_build_profile
from kdive.profiles.types import BuildProfileInput, ExpectedBootFailureInput
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role


@dataclass(frozen=True, slots=True)
class RunReuseRequirementInput:
    """Optional System snapshot assertions for reusing an existing System."""

    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    pcie: list[str] | None = None

    def to_domain(self) -> ReuseRequirement:
        for field_name, value in (
            ("vcpus", self.vcpus),
            ("memory_gb", self.memory_gb),
            ("disk_gb", self.disk_gb),
        ):
            if value is not None and value <= 0:
                raise CategorizedError(
                    "reuse requirement sizing values must be positive",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"field": field_name},
                )
        return ReuseRequirement(
            vcpus=self.vcpus,
            memory_gb=self.memory_gb,
            disk_gb=self.disk_gb,
            pcie=self.pcie or [],
        )


@dataclass(frozen=True, slots=True)
class RunCreateRequest:
    """Validated transport input for creating a Run."""

    investigation_id: str
    system_id: str
    build_profile: BuildProfileInput
    expected_boot_failure: ExpectedBootFailureInput | None = None
    reuse_requirement: RunReuseRequirementInput | None = None

    def domain_reuse_requirement(self) -> ReuseRequirement:
        if self.reuse_requirement is None:
            return ReuseRequirement()
        return self.reuse_requirement.to_domain()


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
) -> ToolResponse:
    """Bind a Run to a `ready` System and an Investigation (ADR-0070 reuse path).

    ``request.reuse_requirement`` lets an agent re-assert, under the lock, the sizing /
    PCIe requirements it discovered via ``systems.list`` — closing the list→create TOCTOU.
    Omitted or empty requirements mean only the unconditional preconditions apply.
    """
    inv_uid = _as_uuid(request.investigation_id)
    if inv_uid is None:
        return _config_error(request.investigation_id)
    sys_uid = _as_uuid(request.system_id)
    if sys_uid is None:
        return _config_error(request.system_id)
    try:
        parsed_build_profile = BuildProfile.parse(request.build_profile)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(request.system_id, exc)
    parsed_expected = _parse_expected_boot_failure(request.system_id, request.expected_boot_failure)
    if isinstance(parsed_expected, ToolResponse):
        return parsed_expected
    try:
        requirement = request.domain_reuse_requirement()
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(request.system_id, exc)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            resolved = await _resolve_targets(conn, ctx, inv_uid, sys_uid)
            if isinstance(resolved, ToolResponse):
                return resolved
            targets, project = resolved
            return await _create_locked(
                conn,
                ctx,
                targets,
                parsed_build_profile,
                parsed_expected,
                project=project,
                requirement=requirement,
            )


async def _resolve_targets(
    conn: AsyncConnection, ctx: RequestContext, inv_uid: UUID, sys_uid: UUID
) -> tuple[_CreateTargets, str] | ToolResponse:
    """Pre-lock fetch + fast-fail checks; resolves the ALLOCATION lock key before locking.

    The allocation id must be known before the first lock (the global order acquires
    ALLOCATION before SYSTEM), so it is read here from the System and carried into the
    locked section, where the allocation is re-read under its lock as the authority.
    """
    inv = await INVESTIGATIONS.get(conn, inv_uid)
    if inv is None or inv.project not in ctx.projects:
        return _config_error(str(inv_uid))
    require_role(ctx, inv.project, Role.OPERATOR)
    system = await SYSTEMS.get(conn, sys_uid)
    if system is None or system.project not in ctx.projects:
        return _config_error(str(sys_uid))
    if system.project != inv.project:
        return _config_error(str(sys_uid))
    alloc = await ALLOCATIONS.get(conn, system.allocation_id)
    if alloc is None or alloc.state not in ALLOC_HOSTABLE:
        current = alloc.state.value if alloc is not None else "missing"
        return _stale_handle(str(sys_uid), current_status=current)
    return _CreateTargets(inv_uid=inv_uid, sys_uid=sys_uid, alloc_uid=alloc.id), inv.project


def _parse_expected_boot_failure(
    object_id: str, value: ExpectedBootFailureInput | None
) -> SerializedExpectedBootFailure | ToolResponse | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    try:
        parsed = ExpectedBootFailure.model_validate(value)
    except ValidationError:
        return _config_error(object_id, data={"reason": "bad_expected_boot_failure"})
    return cast(
        SerializedExpectedBootFailure,
        parsed.model_dump(mode="json", exclude_none=True),
    )


class _CreateTargets:
    """The three locked object ids for a ``runs.create``, carried into the locked section."""

    __slots__ = ("alloc_uid", "inv_uid", "sys_uid")

    def __init__(self, *, inv_uid: UUID, sys_uid: UUID, alloc_uid: UUID) -> None:
        self.inv_uid = inv_uid
        self.sys_uid = sys_uid
        self.alloc_uid = alloc_uid


async def _investigation_for_update(conn: AsyncConnection, uid: UUID) -> Investigation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM investigations WHERE id = %s FOR UPDATE", (uid,))
        row = await cur.fetchone()
    return Investigation.model_validate(row) if row else None


async def _count_non_terminal_runs(conn: AsyncConnection, sys_uid: UUID) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM runs WHERE system_id = %s AND state = ANY(%s)",
            (sys_uid, [s.value for s in RUN_NON_TERMINAL]),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(row[0])


def _system_block_response(system: System | None, sys_uid: UUID) -> ToolResponse | None:
    """Re-validate the System under the lock; return a failure envelope or ``None`` if ok."""
    if system is None:
        return _config_error(str(sys_uid))
    if system.state in SYSTEM_GONE:
        return _stale_handle(str(sys_uid), current_status=system.state.value)
    if system.state not in RUN_HOSTABLE:
        return _config_error(str(sys_uid), data={"current_status": system.state.value})
    return None


def _allocation_block_response(alloc: Allocation | None, sys_uid: UUID) -> ToolResponse | None:
    """Re-validate the Allocation under its lock (live + lease not lapsed), or ``None``.

    A terminal/expiring Allocation (non-``ACTIVE``, or ``ACTIVE`` whose ``lease_expiry`` has
    already elapsed — the ADR-0021 orphan-reaping window) is ``stale_handle`` (ADR-0070).
    """
    if alloc is None:
        return _stale_handle(str(sys_uid), current_status="missing")
    if alloc.state not in ALLOC_HOSTABLE:
        return _stale_handle(str(sys_uid), current_status=alloc.state.value)
    if alloc.lease_expiry is not None and alloc.lease_expiry < datetime.now(UTC):
        return _stale_handle(str(sys_uid), current_status="lease_expired")
    return None


async def _preconditions_block_response(
    conn: AsyncConnection,
    targets: _CreateTargets,
    *,
    project: str,
) -> tuple[ToolResponse, None] | tuple[None, tuple[System, Allocation]]:
    """Run the three unconditional preconditions under the held locks.

    Returns ``(failure, None)`` on a violation, else ``(None, (system, alloc))`` for the
    snapshot assertion and the insert to reuse. Order is fixed — System reachability, live
    allocation, single project, one-Run-per-System — so a stale/conflicting System returns
    its precondition error, never a sizing error (no sizing leak, #166).
    """
    system = await SYSTEMS.get(conn, targets.sys_uid)
    blocked = _system_block_response(system, targets.sys_uid)
    if blocked is not None or system is None:
        return blocked or _config_error(str(targets.sys_uid)), None
    alloc = await ALLOCATIONS.get(conn, targets.alloc_uid)
    blocked = _allocation_block_response(alloc, targets.sys_uid)
    if blocked is not None or alloc is None:
        return blocked or _stale_handle(str(targets.sys_uid), current_status="missing"), None
    if system.project != project:
        return _config_error(str(targets.sys_uid)), None
    if await _count_non_terminal_runs(conn, targets.sys_uid) > 0:
        return (
            ToolResponse.failure(
                str(targets.sys_uid),
                ErrorCategory.TRANSPORT_CONFLICT,
                data={"reason": "system_has_live_run"},
            ),
            None,
        )
    return None, (system, alloc)


def _assertion_block_response(
    system: System, alloc: Allocation, requirement: ReuseRequirement
) -> ToolResponse | None:
    """Apply the optional snapshot-≥ / pcie-contains assertion, or ``None`` if satisfied.

    Checked only after the three preconditions pass (so a stale/conflicting System never
    leaks sizing). A miss — or a malformed / ``class=`` pcie spec — is ``configuration_error``.
    """
    if requirement.is_empty():
        return None
    sizing = read_system_sizing(alloc, system)
    try:
        satisfied = snapshot_satisfies(sizing, alloc.pcie_claim, requirement)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(str(system.id), exc)
    if not satisfied:
        return _config_error(str(system.id), data={"reason": "reuse_requirement_unmet"})
    return None


async def _create_locked(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: _CreateTargets,
    build_profile: ParsedBuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    *,
    project: str,
    requirement: ReuseRequirement,
) -> ToolResponse:
    # Global total lock order PROJECT < RESOURCE < ALLOCATION < SYSTEM, then INVESTIGATION →
    # RUN (locks.py, ADR-0040 §1): ALLOCATION must precede SYSTEM. The reconciler →expired
    # sweep and allocations.release both hold ...ALLOCATION before SYSTEM, so taking SYSTEM
    # first here would deadlock. Acquire ALLOCATION → SYSTEM → INVESTIGATION; the allocation
    # id is resolved pre-lock (create_run) and re-read under its lock as the authority.
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.ALLOCATION, targets.alloc_uid),
        advisory_xact_lock(conn, LockScope.SYSTEM, targets.sys_uid),
        advisory_xact_lock(conn, LockScope.INVESTIGATION, targets.inv_uid),
    ):
        blocked, ok = await _preconditions_block_response(conn, targets, project=project)
        if blocked is not None or ok is None:
            return blocked or _config_error(str(targets.sys_uid))
        system, alloc = ok
        assertion_block = _assertion_block_response(system, alloc, requirement)
        if assertion_block is not None:
            return assertion_block
        inv = await _investigation_for_update(conn, targets.inv_uid)
        if inv is None:
            return _config_error(str(targets.inv_uid))
        if inv.state not in INVESTIGATION_OPEN_FOR_RUN:
            return _config_error(str(targets.inv_uid), data={"current_status": inv.state.value})
        run = await _insert_run(conn, ctx, targets, build_profile, expected_boot_failure, project)
        await _flip_investigation_if_open(conn, ctx, inv, targets.inv_uid, project)
    return _created_response(run, targets, expected_boot_failure, project)


async def _insert_run(
    conn: AsyncConnection,
    ctx: RequestContext,
    targets: _CreateTargets,
    build_profile: ParsedBuildProfile,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    project: str,
) -> Run:
    now = datetime.now(UTC)
    run = await RUNS.insert(
        conn,
        Run(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=project,
            investigation_id=targets.inv_uid,
            system_id=targets.sys_uid,
            state=RunState.CREATED,
            build_profile=dump_build_profile(build_profile),
            expected_boot_failure=expected_boot_failure,
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
            args={"investigation_id": str(targets.inv_uid), "system_id": str(targets.sys_uid)},
            project=project,
        ),
    )
    return run


async def _flip_investigation_if_open(
    conn: AsyncConnection,
    ctx: RequestContext,
    inv: Investigation,
    inv_uid: UUID,
    project: str,
) -> None:
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
    await conn.execute("UPDATE investigations SET last_run_at = now() WHERE id = %s", (inv_uid,))


def _created_response(
    run: Run,
    targets: _CreateTargets,
    expected_boot_failure: SerializedExpectedBootFailure | None,
    project: str,
) -> ToolResponse:
    return ToolResponse.success(
        str(run.id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data={
            "project": project,
            "investigation_id": str(targets.inv_uid),
            "system_id": str(targets.sys_uid),
            **(
                {"expected_boot_failure": str(expected_boot_failure["kind"])}
                if expected_boot_failure is not None
                else {}
            ),
        },
    )
