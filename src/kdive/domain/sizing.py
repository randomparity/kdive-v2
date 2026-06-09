"""Persisted allocation/system sizing helpers (ADR-0067, ADR-0070)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory


@dataclass(frozen=True, slots=True)
class AllocationSizing:
    """Persisted sizing normalized to MB.

    Values come from an Allocation's at-grant snapshot or from concrete sizing stamped into
    a persisted System profile. Reads must not re-resolve shape names from the catalog.
    """

    vcpu: int
    memory_mb: int
    disk_gb: int


def concrete_sizing_from_mapping(profile: Mapping[str, object]) -> AllocationSizing:
    """Read concrete sizing from persisted profile JSON without importing profile schemas.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if any sizing field is missing, null, or
            not an integer.
    """
    values: dict[str, int] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for field in ("vcpu", "memory_mb", "disk_gb"):
        value = profile.get(field)
        if value is None:
            missing.append(field)
        elif type(value) is int:
            values[field] = value
        else:
            invalid.append(field)
    if missing:
        raise CategorizedError(
            f"provisioning profile is missing required sizing: {', '.join(missing)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing": missing},
        )
    if invalid:
        raise CategorizedError(
            f"provisioning profile has non-integer sizing: {', '.join(invalid)}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"invalid": invalid},
        )
    return AllocationSizing(
        vcpu=values["vcpu"],
        memory_mb=values["memory_mb"],
        disk_gb=values["disk_gb"],
    )
