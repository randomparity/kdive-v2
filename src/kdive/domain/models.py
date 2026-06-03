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

``jsonb`` columns whose interior shape is owned by a later issue (capabilities,
provisioning/build profiles, job payload, authorizing tuple) are typed as
``dict[str, Any]`` here; the typed models land with the issues that own them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kdive.domain.errors import ErrorCategory
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

    The spec's "Job queue" section names the five long-running provider ops;
    ``teardown``/``force_crash``/``power`` are also job-dispatched per the tool
    surface (``systems.teardown``/``control.*`` return ``{job_id}``) and the
    implementation plan's ``dedup_key`` set.
    """

    PROVISION = "provision"
    TEARDOWN = "teardown"
    BUILD = "build"
    INSTALL = "install"
    BOOT = "boot"
    FORCE_CRASH = "force_crash"
    POWER = "power"
    CAPTURE_VMCORE = "capture_vmcore"


class Sensitivity(StrEnum):
    """Artifact sensitivity — only a ``redacted`` derivative is response-eligible."""

    SENSITIVE = "sensitive"
    REDACTED = "redacted"


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


class Allocation(DomainModel, _Attribution):
    """An always-yes, capacity-checked booking of a Resource."""

    resource_id: UUID
    state: AllocationState
    lease_expiry: datetime | None = None
    capability_scope: dict[str, Any] = Field(default_factory=dict)


class System(DomainModel, _Attribution):
    """A provisioned target; one per Allocation in M0."""

    allocation_id: UUID
    state: SystemState
    provisioning_profile: dict[str, Any]
    target_fingerprint: str | None = None
    domain_name: str | None = None


class Investigation(DomainModel, _Attribution):
    """A project-scoped campaign grouping Runs toward a goal."""

    title: str
    external_refs: list[ExternalRef] = Field(default_factory=list)
    state: InvestigationState
    last_run_at: datetime | None = None


class Run(DomainModel, _Attribution):
    """One build/install/boot attempt — the join of a System and an Investigation."""

    investigation_id: UUID
    system_id: UUID
    state: RunState
    build_profile: dict[str, Any]
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
    authorizing: dict[str, Any]
    dedup_key: str


class Artifact(DomainModel):
    """A stored object referenced by a System or Run; write-once."""

    owner_kind: str
    owner_id: UUID
    object_key: str
    etag: str
    sensitivity: Sensitivity
    retention_class: str
