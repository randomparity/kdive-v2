"""The system-reuse snapshot-≥ match predicate (ADR-0070, #166).

Reuse attaches a Run to a ``ready`` System the agent did not self-provision. The
*candidacy* predicate (ADR-0070 §Decision) is the persisted **sizing snapshot**, never the
shape name: a System is reusable for a request iff its snapshot sizing **is ≥** the
request's resolved tuple **and** its PCIe claim **contains** the request's required devices.
The "≥" (not exact) is deliberate — the System is already allocated/billed under its own
Allocation, so an over-sized reuse costs the requester nothing extra.

Sizing is read from persisted state only (the Allocation's ``requested_*`` snapshot, else
the System's ``provisioning_profile`` JSON), never re-resolved from the shapes catalog, so a
later ``shapes.set`` cannot retroactively re-size a stamped row (ADR-0067 snapshot identity).

PCIe matching mirrors ``systems.list``'s claim filter: a ``pcie_claim`` entry carries
``(vendor_id, device_id, bdf)`` but **no** ``class_code``, so only a ``vendor:device`` spec
can be honored against it — a ``class=`` spec (or malformed grammar) is a structured
``CONFIGURATION_ERROR``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Allocation, System
from kdive.domain.pcie import PCIeClaim, parse_match_spec
from kdive.profiles.provisioning import (
    AllocationSizing,
    ProvisioningProfile,
    require_concrete_sizing,
)

_MB_PER_GB = 1024
"""Maps the Allocation's GB memory snapshot to the profile's MB sizing (ADR-0067)."""


@dataclass(frozen=True, slots=True)
class ReuseRequirement:
    """An optional reuse-match assertion an agent re-asserts at ``runs.create``.

    Every field is optional: an omitted axis is not asserted, so a partial requirement only
    constrains the axes it names. ``pcie`` is a list of ``vendor:device`` match specs the
    System's claim must contain; an empty list asserts nothing (a no-op, like ``None``).
    """

    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    pcie: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Report whether nothing is asserted (so no snapshot read/match is needed)."""
        return (
            self.vcpus is None and self.memory_gb is None and self.disk_gb is None and not self.pcie
        )


def read_system_sizing(alloc: Allocation, system: System) -> AllocationSizing:
    """Resolve a System's persisted sizing snapshot, normalized to MB (ADR-0067).

    Prefers the Allocation's complete at-grant snapshot (``requested_vcpus`` /
    ``requested_memory_gb`` / ``requested_disk_gb``, GB→MB normalized); falls back to the
    System's ``provisioning_profile`` JSON (already concrete MB sizing) when the snapshot is
    incomplete (a full-custom or legacy allocation). Reads persisted state only — never the
    shapes catalog.

    Args:
        alloc: The System's Allocation (the at-grant sizing snapshot).
        system: The System (its concrete ``provisioning_profile`` sizing).

    Returns:
        The resolved sizing as an :class:`AllocationSizing` (memory in MB).
    """
    if (
        alloc.requested_vcpus is not None
        and alloc.requested_memory_gb is not None
        and alloc.requested_disk_gb is not None
    ):
        return AllocationSizing(
            vcpu=alloc.requested_vcpus,
            memory_mb=alloc.requested_memory_gb * _MB_PER_GB,
            disk_gb=alloc.requested_disk_gb,
        )
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    require_concrete_sizing(profile)  # raises CONFIGURATION_ERROR if any size is NULL
    vcpu, memory_mb, disk_gb = profile.vcpu, profile.memory_mb, profile.disk_gb
    if vcpu is None or memory_mb is None or disk_gb is None:  # pragma: no cover - guarded above
        raise CategorizedError(
            "provisioning profile is missing concrete sizing",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return AllocationSizing(vcpu=vcpu, memory_mb=memory_mb, disk_gb=disk_gb)


def _parse_vendor_device(spec: str) -> tuple[str, str]:
    """Parse a ``vendor:device`` spec, or raise for malformed / unsupported ``class=`` grammar.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``spec`` is malformed or a ``class=``
            spec (claims carry no ``class_code``, so a class match cannot be honored).
    """
    parsed = parse_match_spec(spec)
    if parsed.vendor_id is None or parsed.device_id is None:
        raise CategorizedError(
            f"PCIe match spec {spec!r} is not a vendor:device spec; a class= spec cannot be "
            "matched against an allocation's pcie_claim",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"spec": spec},
        )
    return parsed.vendor_id, parsed.device_id


def _pcie_claims_contain_all(claims: list[PCIeClaim], specs: list[str]) -> bool:
    """Return whether ``claims`` contains a device for each ``vendor:device`` spec.

    Every spec's grammar is validated before any membership check, so a malformed / ``class=``
    spec raises deterministically regardless of an earlier unmatched spec's position.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for any malformed or ``class=`` spec.
    """
    parsed = [_parse_vendor_device(spec) for spec in specs]
    return all(
        any(claim["vendor_id"] == vendor_id and claim["device_id"] == device_id for claim in claims)
        for vendor_id, device_id in parsed
    )


def snapshot_satisfies(
    sizing: AllocationSizing, claims: list[PCIeClaim], req: ReuseRequirement
) -> bool:
    """Report whether a System snapshot satisfies a reuse requirement (≥ / contains).

    Sizing is compared per-asserted-axis with ``≥``; PCIe specs must each be contained in
    ``claims``. An omitted axis or an empty ``pcie`` list is not constrained.

    Args:
        sizing: The System's resolved sizing snapshot (memory in MB).
        claims: The Allocation's ``pcie_claim`` snapshot.
        req: The asserted requirement.

    Returns:
        ``True`` iff every asserted axis is satisfied.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a malformed or ``class=`` pcie spec.
    """
    # Validate every pcie spec's grammar up front (not a short-circuiting any/all) so a
    # malformed/class= spec raises its own error deterministically — even when it follows a
    # valid-but-unmatched spec or a sizing miss that would otherwise return False first.
    pcie_ok = _pcie_claims_contain_all(claims, req.pcie)
    if req.vcpus is not None and sizing.vcpu < req.vcpus:
        return False
    if req.memory_gb is not None and sizing.memory_mb < req.memory_gb * _MB_PER_GB:
        return False
    if req.disk_gb is not None and sizing.disk_gb < req.disk_gb:
        return False
    return pcie_ok
