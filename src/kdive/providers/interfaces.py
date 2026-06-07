"""Shared provider handle and record value types (ADR-0009, ADR-0063).

Production M0/M1 runtime assembly uses typed ``ProviderRuntime`` ports. The capability
registry remains a prototype for future multi-provider dispatch. This module is intentionally
limited to shared cross-provider value aliases and discovery records so it cannot drift from
the realized operation contracts.
"""

from __future__ import annotations

from typing import Any, NewType, TypedDict

from kdive.domain.models import ResourceKind
from kdive.domain.state import ResourceStatus

SystemHandle = NewType("SystemHandle", str)
TransportHandle = NewType("TransportHandle", str)
KernelArtifact = NewType("KernelArtifact", str)
ArtifactRef = NewType("ArtifactRef", str)
BreakpointId = NewType("BreakpointId", str)

type BreakLocation = dict[str, Any]
type Registers = dict[str, Any]


class ResourceRecord(TypedDict):
    """A discovered resource host (Discovery plane)."""

    resource_id: str
    kind: ResourceKind
    capabilities: dict[str, Any]
    status: ResourceStatus


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler (Discovery plane)."""

    system_id: str
    domain_name: str
