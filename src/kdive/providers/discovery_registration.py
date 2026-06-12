"""Provider-owned discovery metadata consumed by service-level registration assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kdive.domain.discovery import DiscoverySource
from kdive.domain.models import ResourceKind


@dataclass(frozen=True, slots=True)
class DiscoveryRegistrationTarget:
    """The discovery source and stable resource id resolved at registration time."""

    discovery: DiscoverySource
    resource_id: str


type DiscoveryTargetFactory = Callable[[], DiscoveryRegistrationTarget]


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryRegistration:
    """Lazy discovery target plus resource registration metadata for one provider."""

    target_factory: DiscoveryTargetFactory
    kind: ResourceKind
    pool_name: str
    cost_class: str
