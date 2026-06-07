"""Shared provider handle and record value types (ADR-0009).

Provider dispatch is modeled by :mod:`kdive.providers.capability`; operation ports live
with the provider implementations that satisfy them. This module is intentionally limited
to shared cross-provider value aliases and discovery records so it cannot drift from the
realized operation contracts.
"""

from __future__ import annotations

from typing import Any, NewType, TypedDict

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
    kind: str
    capabilities: dict[str, Any]
    status: str


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler (Discovery plane)."""

    system_id: str
    domain_name: str
