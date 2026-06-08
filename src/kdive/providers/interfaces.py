"""Shared provider handle and record value types (ADR-0009, ADR-0063).

Production M0/M1 runtime assembly uses typed ``ProviderRuntime`` ports. The capability
registry prototype was removed from production source by ADR-0066. This module is
intentionally limited to shared cross-provider value aliases so it cannot drift from the
realized operation contracts.
"""

from __future__ import annotations

from typing import NewType, TypedDict

SystemHandle = NewType("SystemHandle", str)
TransportHandle = NewType("TransportHandle", str)


class OwnedInfra(TypedDict):
    """Infrastructure a provider owns, for the reconciler (Discovery plane)."""

    system_id: str
    domain_name: str
