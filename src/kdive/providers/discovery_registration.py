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
    """Lazy discovery target plus resource registration metadata for one provider.

    ``creates`` says whether this kind's discovery may **insert** a resource row. It is
    ``False`` for a kind whose existence is owned by the ``systems.toml`` config overlay
    (``reconcile_resources`` is the sole creator — ADR-0112): fault-inject has no host to
    enumerate, so its row exists only when declared in config. A ``creates=False`` discovery
    pass is a bind-only no-op; leaving it ``True`` would let both discovery and the config
    reconcile insert a row for the same host and produce a duplicate.
    """

    target_factory: DiscoveryTargetFactory
    kind: ResourceKind
    pool_name: str
    cost_class: str
    creates: bool = True
