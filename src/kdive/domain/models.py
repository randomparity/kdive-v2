"""Typed records for KDIVE durable objects (ADR-0003, ADR-0005).

Pydantic models matching the Postgres schema. Every tenant object carries the
common identity/timestamp fields
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
:class:`Run` and :class:`Job` is enforced by the repository transition helpers, which set the
category atomically with the terminal transition. A model-level cross-field check would fire
on every field assignment under ``validate_assignment`` and break incremental updates.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kdive.domain.errors import ErrorCategory
from kdive.domain.pcie import PCIeClaim
from kdive.domain.profile_documents import (
    SerializedBuildProfile,
    SerializedExpectedBootFailure,
    SerializedProvisioningProfile,
)
from kdive.domain.sizing import MB_PER_GB
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
    """The provider resource kinds.

    Production defaults to ``LOCAL_LIBVIRT``. ``FAULT_INJECT`` is a concrete opt-in mock
    provider behind the same ``ProviderResolver`` seam and is absent from default
    production composition. ``REMOTE_LIBVIRT`` (ADR-0076) is the M2 remote provider,
    opt-in by operator config (a ``qemu+tls://`` host URI + TLS cert refs).
    """

    LOCAL_LIBVIRT = "local-libvirt"
    FAULT_INJECT = "fault-inject"
    REMOTE_LIBVIRT = "remote-libvirt"


class JobKind(StrEnum):
    """The async job kinds — every tool that returns a ``{job_id}`` handle.

    The spec's "Job queue" section names the long-running provider ops;
    ``teardown``/``force_crash``/``power`` are also job-dispatched per the tool
    surface (``systems.teardown``/``control.*`` return ``{job_id}``) and the
    persisted ``dedup_key`` contract. ``reprovision`` is the in-place reprovision op
    (ADR-0038), long-running like ``provision``.
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
    IMAGE_BUILD = "image_build"


type DestructiveJobKind = Literal[
    JobKind.REPROVISION,
    JobKind.TEARDOWN,
    JobKind.FORCE_CRASH,
    JobKind.POWER,
]


class ImageVisibility(StrEnum):
    """Resolution scope of an image_catalog row (ADR-0092/0093).

    ``PUBLIC`` images resolve for every project; a ``PRIVATE`` image resolves only within
    its owning project and shadows a same-identity public image there.
    """

    PUBLIC = "public"
    PRIVATE = "private"


class ImageState(StrEnum):
    """Publish lifecycle of an image_catalog row (ADR-0092).

    ``DEFINED`` is seeded baseline metadata with no object yet; ``PENDING`` is a publish in
    flight (row written, object not yet HEAD-confirmed); ``REGISTERED`` is bootable.
    Resolution returns only ``REGISTERED`` rows.
    """

    DEFINED = "defined"
    PENDING = "pending"
    REGISTERED = "registered"


class ManagedBy(StrEnum):
    """Row-ownership partition for reconciled inventory tables (ADR-0112).

    ``CONFIG`` rows are owned by declarative ``systems.toml`` bring-up; ``DISCOVERY`` rows are
    owned by provider discovery; ``RUNTIME`` rows are owned by imperative agent tools. The
    partition keeps declarative reconcile and imperative registration from pruning or
    overwriting each other's rows.
    """

    CONFIG = "config"
    DISCOVERY = "discovery"
    RUNTIME = "runtime"


class PowerAction(StrEnum):
    """Power operations accepted by the durable control-plane job contract."""

    ON = "on"
    OFF = "off"
    CYCLE = "cycle"
    RESET = "reset"


class JobAuthorizing(TypedDict):
    principal: str
    agent_session: str | None
    project: str


class Sensitivity(StrEnum):
    """Artifact sensitivity — only a ``redacted`` derivative is response-eligible.

    ``quarantined`` is a raw artifact written before secret registration completed
    (ADR-0075): excluded from every serve gate exactly like ``sensitive``, but marking an
    unfulfilled redaction obligation the op heals to a ``redacted`` sibling before release.
    """

    SENSITIVE = "sensitive"
    REDACTED = "redacted"
    QUARANTINED = "quarantined"


class LedgerEventType(StrEnum):
    """The two signed metering events on the ledger (ADR-0007 §3).

    ``reserved`` is the at-grant debit (`+estimate`); ``reconciled`` is the
    at-release/expiry adjustment (`actual − Σ reserved`, which may be negative — a
    credit for an unused reservation window). The signed ``event_type`` column leaves
    room for later per-operation surcharges without a migration.
    """

    RESERVED = "reserved"
    RECONCILED = "reconciled"


class _DomainBase(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DomainModel(_DomainBase):
    """Identity and timestamps common to every durable object."""

    id: UUID
    created_at: datetime
    updated_at: datetime


class _Attribution(_DomainBase):
    """The attribution tuple recorded for tenant-owned objects."""

    principal: str
    agent_session: str | None = None
    project: str


class ExternalRef(_DomainBase):
    """A mutable link to an external tracker (e.g. bugzilla, jira)."""

    tracker: str
    id: str
    url: str


class Resource(DomainModel):
    """A registered provider resource host.

    ``managed_by`` partitions ownership between declarative bring-up (``config``), discovery
    (``discovery``), and imperative agent tools (``runtime``) so the inventory reconciler and
    runtime registration own disjoint row-sets (ADR-0112). ``name`` is a mutable stable
    identity (unique per ``kind`` when present; the ``id`` UUID stays the PK). ``owner_project``
    (``None`` = global) with ``affinity_allowlist`` scopes a resource to specific projects, and
    ``lease_expires_at`` backs leak-reaping of runtime-registered resources.
    """

    kind: ResourceKind
    capabilities: dict[str, Any] = Field(default_factory=dict)
    pool: str
    cost_class: str
    status: ResourceStatus
    host_uri: str
    cordoned: bool = False
    managed_by: ManagedBy = ManagedBy.RUNTIME
    name: str | None = None
    owner_project: str | None = None
    affinity_allowlist: list[str] = Field(default_factory=list)
    lease_expires_at: datetime | None = None


class Allocation(DomainModel, _Attribution):
    """A capacity- and budget-checked booking of a Resource.

    The selector size is persisted at grant (``requested_vcpus``,
    ``requested_memory_gb``, and ``requested_disk_gb``) so accounting, availability, and reuse
    do not depend on mutable catalog state. The billing interval is ``active_started_at`` to
    ``active_ended_at`` and is never derived from ``updated_at`` (ADR-0007 §3).

    ``shape`` records the named preset a shape-sized request resolved from (``None`` for
    full-custom). It is a label, not a foreign key: later shape edits cannot re-size a
    stamped row because sizing reads from this persisted snapshot (ADR-0067).

    ``pcie_claim`` is the resolved list of ``(vendor_id, device_id, bdf)`` devices held by
    this allocation (ADR-0068). Occupancy is derived from this column on non-terminal
    allocations, so the claim frees on terminal transition while the historical row keeps
    the snapshot.

    ``requested`` is the durable queue state for capacity-denied requests (ADR-0069). A
    queued row holds only queue position: ``resource_id`` is ``None``, no reserve or lease is
    held, ``pcie_claim`` is empty, and the original request inputs are persisted for
    promotion re-admission.
    """

    resource_id: UUID | None = None
    state: AllocationState
    lease_expiry: datetime | None = None
    capability_scope: dict[str, Any] = Field(default_factory=dict)
    requested_vcpus: int | None = None
    requested_memory_gb: int | None = None
    requested_disk_gb: int | None = None
    shape: str | None = None
    active_started_at: datetime | None = None
    active_ended_at: datetime | None = None
    pcie_claim: list[PCIeClaim] = Field(default_factory=list)
    requested_pcie_specs: list[str] = Field(default_factory=list)
    requested_kind: ResourceKind | None = None
    requested_resource_id: UUID | None = None


class System(DomainModel, _Attribution):
    """A provisioned target; one per Allocation.

    The nullable ``shape`` label is ``None`` for full-custom allocations (ADR-0067). The
    System's size of record lives in ``provisioning_profile`` (vcpu/memory_mb/disk_gb), so a
    catalog change never re-sizes it.
    """

    allocation_id: UUID
    state: SystemState
    provisioning_profile: SerializedProvisioningProfile
    target_fingerprint: str | None = None
    domain_name: str | None = None
    shape: str | None = None


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


class ImageCatalogEntry(DomainModel):
    """One catalog image row — the single source of truth for a bootable rootfs (ADR-0092).

    Identity is ``(provider, name, arch)`` plus the boot layout (``format``, ``root_device``).
    ``object_key`` is the object-store key of the qcow2 — ``None`` for a ``DEFINED`` row whose
    bytes are not built yet — and ``digest`` is the qcow2 content digest (a rootfs image has no
    kernel ``build_id``), ``None`` until built. ``visibility``/``owner``/``expires_at`` express
    the public-vs-project-private scope (ADR-0093); the DB ``CHECK`` constraints tie ``owner``
    and ``expires_at`` to the private case and ``object_key`` to the non-``DEFINED`` case.
    ``pending_since`` backs the publish-deadline grace window the reconciler keys off.
    """

    provider: str
    name: str
    arch: str
    format: str
    root_device: str
    object_key: str | None = None
    digest: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    visibility: ImageVisibility
    owner: str | None = None
    expires_at: datetime | None = None
    state: ImageState = ImageState.DEFINED
    pending_since: datetime
    managed_by: ManagedBy = ManagedBy.RUNTIME
    volume: str | None = None


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

    ``max_pending_allocations`` is a distinct per-project cap on queued ``requested`` rows
    (ADR-0069), bounding how deep one project can fill the backlog with
    ``on_capacity=queue``. It is separate from ``max_concurrent_allocations``, which does not
    count queued requests; the default 0 keeps queueing opt-in and fail-closed until an
    operator raises it.
    """

    project: str
    max_concurrent_allocations: int
    max_concurrent_systems: int
    max_pending_allocations: int = 0
    updated_at: datetime


class SystemShape(_DomainBase):
    """One named sizing preset in the shapes catalog (ADR-0067).

    Keyed by ``name`` (PK), seeded by migration 0013 with ``small`` / ``medium`` /
    ``large`` / ``max``. A shape fixes **size only** — ``vcpus`` / ``memory_mb`` /
    ``disk_gb`` plus an optional ``pcie_match``; ``cost_class`` is resolved host-side at
    admission, not carried here. ``memory_mb`` is constrained to whole-GB multiples so the
    resolver maps ``memory_mb → memory_gb`` exactly (the same constraint the migration's
    CHECK enforces). ``pcie_match`` is stored opaquely until the matcher grammar lands.
    """

    name: str
    vcpus: int = Field(gt=0, strict=True)
    memory_mb: int = Field(gt=0, strict=True)
    disk_gb: int = Field(gt=0, strict=True)
    pcie_match: str | None = None
    updated_at: datetime

    @field_validator("memory_mb")
    @classmethod
    def _whole_gb(cls, value: int) -> int:
        if value % MB_PER_GB != 0:
            raise ValueError(f"memory_mb {value} must be a whole-GB multiple of {MB_PER_GB}")
        return value


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
