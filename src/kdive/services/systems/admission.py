"""System define/provision admission service (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.jobs.handlers.systems``.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, Job, JobKind, System
from kdive.domain.sizing import MB_PER_GB, AllocationSizing
from kdive.domain.state import AllocationState, IllegalTransition, SystemState
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import SystemPayload
from kdive.profiles.provider_policy import reject_rootfs_upload_without_window
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    dump_profile,
    reconcile_profile_sizing,
    require_concrete_sizing,
)
from kdive.profiles.types import ProvisioningProfileInput
from kdive.provider_components.validation import ComponentSourceCapabilities
from kdive.providers.runtime import ProfilePolicy
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.systems.validation import (
    RootfsValidator,
    validate_profile_for_provider,
    validate_rootfs_for_provider,
)

# System states that occupy a per-project quota slot (terminal torn_down/failed do not).
_NON_TERMINAL_SYSTEM = (
    SystemState.DEFINED,  # the create-without-provision producer (systems.define)
    SystemState.PROVISIONING,
    SystemState.READY,
    SystemState.REPROVISIONING,
    SystemState.CRASHED,
)
type LockedAllocationSystem = tuple[AsyncConnection, Allocation, System | None]
type CreateSystemMode = Literal["provision", "define"]


@dataclass(frozen=True, slots=True)
class MissingAllocation:
    """A not-found or out-of-scope Allocation encountered while acquiring locks."""

    allocation_id: UUID


@dataclass(frozen=True, slots=True)
class CreateSystemRequest:
    allocation_id: UUID
    profile: ProvisioningProfileInput
    mode: CreateSystemMode


@dataclass(frozen=True, slots=True)
class ProvisionDefinedRequest:
    system_id: UUID


@dataclass(frozen=True, slots=True)
class AdmissionFailure:
    object_id: str
    category: ErrorCategory
    data: dict[str, object]
    suggested_next_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProvisionJobAdmitted:
    job: Job
    system_id: UUID


@dataclass(frozen=True, slots=True)
class DefinedSystemAdmitted:
    system: System


type AdmissionResult = AdmissionFailure | ProvisionJobAdmitted | DefinedSystemAdmitted


def _failure(
    object_id: str | UUID,
    category: ErrorCategory = ErrorCategory.CONFIGURATION_ERROR,
    *,
    data: dict[str, object] | None = None,
    suggested_next_actions: tuple[str, ...] = (),
) -> AdmissionFailure:
    return AdmissionFailure(
        object_id=str(object_id),
        category=category,
        data=data or {},
        suggested_next_actions=suggested_next_actions,
    )


def _safe_error_details(details: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in details.items():
        if isinstance(value, float):
            if math.isfinite(value):
                safe[key] = value
            continue
        if isinstance(value, (str, bool, int)):
            safe[key] = value
    return safe


def _failure_from_error(object_id: str | UUID, exc: CategorizedError) -> AdmissionFailure:
    return _failure(object_id, exc.category, data=_safe_error_details(exc.details))


def _stored_profile_for(
    profile: ProvisioningProfileInput, alloc: Allocation
) -> ProvisioningProfile:
    """Resolve the concrete profile to store for ``alloc`` (ADR-0067, ADR-0024 delta).

    When the Allocation carries a complete resolved-sizing snapshot (``requested_vcpus`` /
    ``requested_memory_gb`` / ``requested_disk_gb``), the profile sizing is reconciled
    against it — filled when omitted, rejected when conflicting — so admitted size equals
    booted size. When the snapshot is incomplete (a full-custom or legacy allocation), the
    profile must carry its own concrete sizing. Either way the stored profile is concrete,
    so the libvirt renderer never reads a ``None`` size.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on a conflicting restatement or a profile
            with missing sizing in the no-snapshot lane.
    """
    if (
        alloc.requested_vcpus is not None
        and alloc.requested_memory_gb is not None
        and alloc.requested_disk_gb is not None
    ):
        reconciled = reconcile_profile_sizing(
            profile,
            AllocationSizing(
                vcpu=alloc.requested_vcpus,
                memory_mb=alloc.requested_memory_gb * MB_PER_GB,
                disk_gb=alloc.requested_disk_gb,
            ),
        )
        return ProvisioningProfile.parse(reconciled)
    parsed = ProvisioningProfile.parse(profile)
    require_concrete_sizing(parsed)
    return parsed


@dataclass(frozen=True, slots=True)
class SystemAdmission:
    """Admission service with provider validation seams bound at construction."""

    profile_policy: ProfilePolicy
    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    async def provision_defined(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        request: ProvisionDefinedRequest,
    ) -> AdmissionResult:
        """Admit a ``defined`` System after its upload window is complete."""
        return await _provision_defined_locked(
            pool,
            ctx,
            request.system_id,
            profile_policy=self.profile_policy,
            component_sources=self.component_sources,
            rootfs_validator=self.rootfs_validator,
        )

    async def create_for_allocation(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        request: CreateSystemRequest,
    ) -> AdmissionResult:
        """Validate and lock the shared create-lane admission path.

        The submitted profile is structurally pre-parsed first (sizing now optional,
        ADR-0067) for early provider/rootfs validation. The sizing is reconciled against the
        Allocation's resolved snapshot inside the lock — once ``alloc`` is in scope — at the
        single create-insert point (:func:`_stored_profile_for`), so the stored profile is
        always concrete and admitted size equals booted size.
        """
        try:
            parsed = ProvisioningProfile.parse(request.profile)
            validate_profile_for_provider(parsed, self.profile_policy, self.component_sources)
        except CategorizedError as exc:
            return _failure_from_error(request.allocation_id, exc)
        try:
            async with _locked_allocation_system(pool, ctx, request.allocation_id) as locked:
                if isinstance(locked, MissingAllocation):
                    return _failure(locked.allocation_id)
                conn, alloc, existing = locked
                try:
                    stored = _stored_profile_for(request.profile, alloc)
                except CategorizedError as exc:
                    return _failure_from_error(alloc.id, exc)
                if request.mode == "provision":
                    return await _provision_create_response(
                        conn,
                        ctx,
                        alloc,
                        existing,
                        profile=stored,
                        profile_policy=self.profile_policy,
                        rootfs_validator=self.rootfs_validator,
                    )
                return await _define_create_response(
                    conn,
                    ctx,
                    alloc,
                    existing,
                    profile=stored,
                    profile_policy=self.profile_policy,
                    rootfs_validator=self.rootfs_validator,
                )
        except IllegalTransition:
            async with pool.connection() as conn:
                latest = await ALLOCATIONS.get(conn, request.allocation_id)
            data: dict[str, object] = {"current_status": latest.state.value} if latest else {}
            return _failure(request.allocation_id, data=data)


async def _within_system_quota(conn: AsyncConnection, project: str) -> bool:
    """Report whether the project is under ``max_concurrent_systems`` (ADR-0007 §4).

    Fail-closed: a project with **no quota row** is over quota (no silent default).
    Counts the project's non-terminal Systems under the held PROJECT lock, so the
    count-then-create cannot overshoot under concurrent provisions.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT max_concurrent_systems FROM quotas WHERE project = %s", (project,)
        )
        row = await cur.fetchone()
    if row is None:
        return False
    cap = int(row[0])
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM systems WHERE project = %s AND state = ANY(%s)",
            (project, [s.value for s in _NON_TERMINAL_SYSTEM]),
        )
        count_row = await cur.fetchone()
    if count_row is None:  # Invariant: count(*) always yields a row.
        raise RuntimeError("count(*) returned no row")
    return int(count_row[0]) < cap


async def _find_system_for_allocation(conn: AsyncConnection, alloc_id: UUID) -> System | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM systems WHERE allocation_id = %s ORDER BY created_at, id LIMIT 1",
            (alloc_id,),
        )
        row = await cur.fetchone()
    return System.model_validate(row) if row else None


@asynccontextmanager
async def _locked_allocation_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    alloc_id: UUID,
) -> AsyncIterator[LockedAllocationSystem | MissingAllocation]:
    # Resolve the allocation's project (immutable) before locking so the PROJECT lock key
    # is known up front; a missing/foreign allocation is a not-found-shaped config error.
    async with pool.connection() as probe:
        probe_alloc = await ALLOCATIONS.get(probe, alloc_id)
    if probe_alloc is None or probe_alloc.project not in ctx.projects:
        yield MissingAllocation(alloc_id)
        return
    project = probe_alloc.project
    # PROJECT → ALLOCATION (the global lock order, ADR-0040 §1): the project lock so the
    # max_concurrent_systems count-then-create is race-free against a concurrent provision,
    # the allocation lock so a release-mid-provision cannot leak a domain.
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, alloc_id),
    ):
        alloc = await ALLOCATIONS.get(conn, alloc_id)
        if alloc is None or alloc.project not in ctx.projects:
            yield MissingAllocation(alloc_id)
            return
        require_role(ctx, alloc.project, Role.OPERATOR)
        existing = await _find_system_for_allocation(conn, alloc_id)
        yield conn, alloc, existing


async def _provision_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    if existing is None:
        return await _insert_provisioning_system(
            conn, ctx, alloc, profile, profile_policy, rootfs_validator
        )
    if existing.state is SystemState.DEFINED:
        return _failure(
            existing.id,
            data={
                "current_status": existing.state.value,
                "reason": "use_systems.provision_defined",
            },
        )
    if existing.state is SystemState.PROVISIONING:
        return await _enqueue_provision_job(
            conn,
            ctx,
            project=alloc.project,
            allocation_id=alloc.id,
            system_id=existing.id,
        )
    return _failure(existing.id, data={"current_status": existing.state.value})


async def _define_create_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    existing: System | None,
    *,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    if existing is None:
        return await _insert_defined_system(
            conn, ctx, alloc, profile, profile_policy, rootfs_validator
        )
    if existing.state is SystemState.DEFINED:
        return DefinedSystemAdmitted(existing)  # idempotent re-define
    return _failure(existing.id, data={"current_status": existing.state.value})


async def _admit_defined(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    system: System,
) -> AdmissionResult:
    """Drive a ``defined`` System ``defined -> provisioning`` and enqueue its provision job.

    The stored profile is provisioned (ADR-0025 decision 7); the Allocation is already
    ``active`` (flipped at ``define``), so it is not touched. Keyed on the allocation, so
    a retried ``systems.provision`` dedups to the same job.
    """
    await SYSTEMS.update_state(conn, system.id, SystemState.PROVISIONING)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool="systems.provision",
            object_kind="systems",
            object_id=system.id,
            transition="defined->provisioning",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return await _enqueue_provision_job(
        conn,
        ctx,
        project=alloc.project,
        allocation_id=alloc.id,
        system_id=system.id,
    )


async def _enqueue_provision_job(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    project: str,
    allocation_id: UUID,
    system_id: UUID,
) -> ProvisionJobAdmitted:
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system_id)),
        job_authorizing(ctx, project),
        f"{allocation_id}:provision",
    )
    return ProvisionJobAdmitted(job=job, system_id=system_id)


async def _provision_defined_locked(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: UUID,
    *,
    profile_policy: ProfilePolicy,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    async with pool.connection() as probe:
        probe_system = await SYSTEMS.get(probe, system_id)
        if probe_system is None or probe_system.project not in ctx.projects:
            return _failure(system_id)
        project = probe_system.project
        allocation_id = probe_system.allocation_id
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.PROJECT, project),
        advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id),
    ):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.project not in ctx.projects:
            return _failure(system_id)
        require_role(ctx, system.project, Role.OPERATOR)
        alloc = await ALLOCATIONS.get(conn, system.allocation_id)
        if alloc is None or alloc.project != system.project:
            return _failure(system.allocation_id)
        return await _provision_defined_response(
            conn,
            ctx,
            system,
            alloc,
            profile_policy=profile_policy,
            component_sources=component_sources,
            rootfs_validator=rootfs_validator,
        )


async def _provision_defined_response(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    alloc: Allocation,
    *,
    profile_policy: ProfilePolicy,
    component_sources: ComponentSourceCapabilities,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    if system.state is SystemState.PROVISIONING:
        return await _enqueue_provision_job(
            conn,
            ctx,
            project=system.project,
            allocation_id=system.allocation_id,
            system_id=system.id,
        )
    if system.state is not SystemState.DEFINED:
        return _failure(system.id, data={"current_status": system.state.value})
    try:
        parsed = ProvisioningProfile.parse(system.provisioning_profile)
        validate_profile_for_provider(parsed, profile_policy, component_sources)
        validate_rootfs_for_provider(parsed, profile_policy, rootfs_validator)
    except CategorizedError as exc:
        return _failure_from_error(system.id, exc)
    if alloc.state is not AllocationState.ACTIVE:
        return _failure(alloc.id, data={"current_status": alloc.state.value})
    return await _admit_defined(conn, ctx, alloc, system)


async def _new_system_allowed(
    conn: AsyncConnection,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionFailure | None:
    if alloc.state is not AllocationState.GRANTED:
        return _failure(alloc.id, data={"current_status": alloc.state.value})
    # New System: enforce the per-project max_concurrent_systems quota under the held
    # project lock. Fail-closed — no quota row → denied (ADR-0007 §4); a denial writes
    # no System, no job, and leaves the allocation granted (the all-or-nothing rule).
    if not await _within_system_quota(conn, alloc.project):
        return _failure(
            alloc.id,
            ErrorCategory.QUOTA_EXCEEDED,
            suggested_next_actions=("systems.get", "allocations.list"),
        )
    try:
        validate_rootfs_for_provider(profile, profile_policy, rootfs_validator)
    except CategorizedError as exc:
        return _failure_from_error(alloc.id, exc)
    return None


async def _insert_system_and_activate(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    *,
    state: SystemState,
    tool: str,
    transition: str,
) -> System:
    now = datetime.now(UTC)  # placeholder; the DB sets created_at/updated_at
    system = await SYSTEMS.insert(
        conn,
        System(
            id=uuid4(),
            created_at=now,
            updated_at=now,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            project=alloc.project,
            allocation_id=alloc.id,
            state=state,
            provisioning_profile=dump_profile(profile),
            shape=alloc.shape,
        ),
    )
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=system.id,
            transition=transition,
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.ACTIVE)
    await audit.record(
        conn,
        ctx,
        audit.AuditEvent(
            tool=tool,
            object_kind="allocations",
            object_id=alloc.id,
            transition="granted->active",
            args={"allocation_id": str(alloc.id)},
            project=alloc.project,
        ),
    )
    return system


async def _insert_defined_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    blocked = await _new_system_allowed(conn, alloc, profile, profile_policy, rootfs_validator)
    if blocked is not None:
        return blocked
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.DEFINED,
        tool="systems.define",
        transition="->defined",
    )
    return DefinedSystemAdmitted(system)


async def _insert_provisioning_system(
    conn: AsyncConnection,
    ctx: RequestContext,
    alloc: Allocation,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    rootfs_validator: RootfsValidator,
) -> AdmissionResult:
    try:
        reject_rootfs_upload_without_window(profile_policy, profile)
    except CategorizedError as exc:
        return _failure_from_error(alloc.id, exc)
    blocked = await _new_system_allowed(conn, alloc, profile, profile_policy, rootfs_validator)
    if blocked is not None:
        return blocked
    system = await _insert_system_and_activate(
        conn,
        ctx,
        alloc,
        profile,
        state=SystemState.PROVISIONING,
        tool="systems.provision",
        transition="->provisioning",
    )
    job = await queue.enqueue(
        conn,
        JobKind.PROVISION,
        SystemPayload(system_id=str(system.id)),
        job_authorizing(ctx, alloc.project),
        f"{alloc.id}:provision",
    )
    return ProvisionJobAdmitted(job=job, system_id=system.id)
