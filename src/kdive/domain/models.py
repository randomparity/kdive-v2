"""Typed records for the M0 durable objects (ADR-0003, ADR-0005).

Pydantic models matching the spec's Postgres schema ("Postgres schema (M0
subset)"). Every tenant object carries the common identity/timestamp fields
(:class:`DomainModel`) and the ``(principal, agent_session, project)`` attribution
tuple (:class:`_Attribution`). Three objects deviate, per their schema rows:

* :class:`Resource` is infrastructure — it has a health ``status`` (not a tenant
  ``state``) and no attribution.
* :class:`Job` records its authorizing tuple in the ``authorizing`` jsonb column
  rather than as attribution columns.
* :class:`Artifact` is a write-once record — no lifecycle ``state``.

``jsonb`` columns whose interior shape is open-ended (capabilities, job payload,
authorizing tuple) remain typed as ``dict[str, Any]`` here. Profile-owned JSON
columns use profile document aliases and are parsed by their owning profile modules.

The "failure category set iff the object reached a failure state" invariant on
:class:`Run` and :class:`Job` is not enforced at this layer. The repository
(issue #5) sets the category atomically with the terminal transition; a
model-level cross-field check would fire on every field assignment under
``validate_assignment`` and break incremental updates.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.domain.errors import ErrorCategory
from kdive.domain.profile_documents import (
    SerializedBuildProfile,
    SerializedExpectedBootFailure,
    SerializedProvisioningProfile,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)


class ResourceKind(StrEnum):
    """The provider resource kinds; M0 ships one."""

    LOCAL_LIBVIRT = "local-libvirt"


class JobKind(StrEnum):
    """The async job kinds — every tool that returns a ``{job_id}`` handle.

    The spec's "Job queue" section names the long-running provider ops;
    ``teardown``/``force_crash``/``power`` are also job-dispatched per the tool
    surface (``systems.teardown``/``control.*`` return ``{job_id}``) and the
    implementation plan's ``dedup_key`` set. ``reprovision`` is the M1 in-place
    reprovision op (ADR-0038), long-running like ``provision``.
    """

    PROVISION = "provision"
    REPROVISION = "reprovision"
    TEARDOWN = "teardown"
    BUILD = "build"
    INSTALL = "install"
    BOOT = "boot"
    FORCE_CRASH = "force_crash"
    POWER = "power"
    CAPTURE_VMCORE = "capture_vmcore"


class PowerAction(StrEnum):
    """Power operations accepted by the durable control-plane job contract."""

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"


class JobAuthorizing(TypedDict):
    """The fixed authorizing tuple persisted with every durable job."""

    principal: str
    agent_session: str | None
    project: str


class Sensitivity(StrEnum):
    """Artifact sensitivity — only a ``redacted`` derivative is response-eligible."""

    SENSITIVE = "sensitive"
    REDACTED = "redacted"


class LedgerEventType(StrEnum):
    """The two signed metering events on the M1 ledger (ADR-0007 §3).

    ``reserved`` is the at-grant debit (`+estimate`); ``reconciled`` is the
    at-release/expiry adjustment (`actual − Σ reserved`, which may be negative — a
    credit for an unused reservation window). The signed ``event_type`` column leaves
    room for later per-operation surcharges without a migration.
    """

    RESERVED = "reserved"
    RECONCILED = "reconciled"


class _DomainBase(BaseModel):
    """Shared Pydantic config: reject unknown fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DomainModel(_DomainBase):
    """Identity and timestamps common to every durable object."""

    id: UUID
    created_at: datetime
    updated_at: datetime


class _Attribution(_DomainBase):
    """The attribution tuple; ``agent_session`` is optional in M0 (principal-only)."""

    principal: str
    agent_session: str | None = None
    project: str


class ExternalRef(_DomainBase):
    """A mutable link to an external tracker (e.g. bugzilla, jira)."""

    tracker: str
    id: str
    url: str


class Resource(DomainModel):
    """A registered provider host (the local libvirt host in M0)."""

    kind: ResourceKind
    capabilities: dict[str, Any] = Field(default_factory=dict)
    pool: str
    cost_class: str
    status: ResourceStatus
    host_uri: str
    cordoned: bool = False


class Allocation(DomainModel, _Attribution):
    """A capacity- and budget-checked booking of a Resource.

    M1 adds the selector size persisted at grant (``requested_vcpus`` /
    ``requested_memory_gb``, the rate inputs reconciliation recomputes from) and the
    billing interval (``active_started_at`` stamped on ``granted → active``,
    ``active_ended_at`` on release/expiry); ``active_hours`` is their difference, never
    derived from ``updated_at`` (ADR-0007 §3). All four are null on an M0/just-granted
    allocation.
    """

    resource_id: UUID
    state: AllocationState
    lease_expiry: datetime | None = None
    capability_scope: dict[str, Any] = Field(default_factory=dict)
    requested_vcpus: int | None = None
    requested_memory_gb: int | None = None
    active_started_at: datetime | None = None
    active_ended_at: datetime | None = None


class System(DomainModel, _Attribution):
    """A provisioned target; one per Allocation in M0."""

    allocation_id: UUID
    state: SystemState
    provisioning_profile: SerializedProvisioningProfile
    target_fingerprint: str | None = None
    domain_name: str | None = None


class Investigation(DomainModel, _Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None


class ExpectedBootFailure(_DomainBase):
    """Run-scoped expected boot failure metadata (ADR-0064)."""

    kind: Literal["console_crash"]
    pattern: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=256)

    @field_validator("pattern")
    @classmethod
    def _literal_or_pattern(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("pattern must not contain NUL")
        terms = value.split("|")
        if any(term == "" for term in terms):
            raise ValueError("pattern contains an empty term")
        if len(terms) > 16:
            raise ValueError("pattern has too many terms")
        return value


class Run(DomainModel, _Attribution):
    """One build/install/boot attempt — the join of a System and an Investigation."""

    investigation_id: UUID
    system_id: UUID
    state: RunState
    build_profile: SerializedBuildProfile
    expected_boot_failure: SerializedExpectedBootFailure | None = None
    kernel_ref: str | None = None
    debuginfo_ref: str | None = None
    failure_category: ErrorCategory | None = None


class DebugSession(DomainModel, _Attribution):
    """One boot's debug attachment over a transport."""

    run_id: UUID
    state: DebugSessionState
    transport: str
    transport_handle: str | None = None
    worker_heartbeat_at: datetime | None = None


class Job(DomainModel):
    """A durable unit of async work; the ``jobs`` table is the queue."""

    kind: JobKind
    payload: dict[str, Any] = Field(default_factory=dict)
    state: JobState
    attempt: int = 0
    max_attempts: int
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_ref: str | None = None
    error_category: ErrorCategory | None = None
    failure_context: dict[str, str] = Field(default_factory=dict)
    authorizing: JobAuthorizing
    dedup_key: str


class Artifact(DomainModel):
    """A stored object referenced by a System or Run; write-once."""

    owner_kind: str
    owner_id: UUID
    object_key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str


class CostClassCoefficient(_DomainBase):
    """One row of the per-``cost_class`` cost multiplier table (ADR-0007 §1).

    Keyed by ``cost_class`` (PK), seeded with ``('local', 1.0)`` by migration 0002.
    Adding a future provider adds a row, not a cost-model branch. ``coeff`` is
    ``numeric`` in Postgres, carried as :class:`~decimal.Decimal` so cost arithmetic
    stays exact.
    """

    cost_class: str
    coeff: Decimal
    updated_at: datetime


class Budget(_DomainBase):
    """A project's spend budget with the O(1) running spent total (ADR-0007 §3).

    Keyed by ``project`` (PK). ``budget_remaining = limit_kcu − spent_kcu``; ``spent_kcu``
    is the running total every ledger write adjusts under the project lock, so admission
    reads it without summing the append-only ledger. No budget row → the project is
    denied (read as ``limit_kcu = 0``); a deployment seeds it explicitly.
    """

    project: str
    limit_kcu: Decimal
    spent_kcu: Decimal = Decimal(0)
    updated_at: datetime


class Quota(_DomainBase):
    """A project's two concurrency caps (ADR-0007 §4).

    Keyed by ``project`` (PK). ``max_concurrent_allocations`` is checked at
    ``allocations.request``; ``max_concurrent_systems`` at ``systems.provision``. No
    quota row → the project is denied (``quota_exceeded``); a deployment seeds it
    explicitly.
    """

    project: str
    max_concurrent_allocations: int
    max_concurrent_systems: int
    updated_at: datetime


class LedgerEntry(_DomainBase):
    """One append-only, signed metering row (ADR-0007 §3).

    The ledger is the audit trail and the ``by_cost_class`` source for
    ``accounting.usage``. ``kcu_delta`` is signed (a ``reconciled`` credit is negative);
    rows are immutable and ordered by ``ts`` (no ``updated_at``). ``resource_id`` is
    nullable for a credit that reconciles an allocation released before any System was
    provisioned.
    """

    id: UUID
    ts: datetime
    project: str
    allocation_id: UUID
    resource_id: UUID | None = None
    cost_class: str
    event_type: LedgerEventType
    kcu_delta: Decimal
    note: str | None = None
