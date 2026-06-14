"""Fault-inject Discovery plane (ADR-0071, ADR-0072, ADR-0112).

`FaultInjectDiscovery` describes the synthetic resource's runtime engine config — there is
no host to enumerate. Its ``capabilities`` jsonb carries the per-plane concurrent-allocation
cap plus the fault-engine keys (``seed`` / ``fault_rate`` / ``max_latency_s`` /
``secret_ref``); the seeded engine and forced secret resolution read those keys. Happy-path
discovery writes an empty ``fault_rate``/``max_latency_s`` so no fault is drawn until a
deployment configures one.

The billable sizing (``vcpus`` / ``memory_mb``) is **not** advertised here — it comes from
the ``systems.toml`` config overlay (``reconcile_resources``, ADR-0112 #393), which is the
sole creator of the fault-inject resource row. Discovery is bind-only (``creates=False``).
"""

from __future__ import annotations

from typing import Any

import kdive.config as config
from kdive.config.registry import Setting
from kdive.domain.discovery import ResourceRecord
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    MAX_LATENCY_S_KEY,
    SECRET_REF_KEY,
    SEED_KEY,
)
from kdive.providers.fault_inject.settings import (
    FAULT_INJECT_ALLOCATION_CAP,
    FAULT_INJECT_SECRET_REF,
    FAULT_INJECT_SEED,
    FAULT_INJECT_URI,
)


def _int_setting(setting: Setting[str]) -> int:
    """Resolve an integer setting, raising ``CONFIGURATION_ERROR`` on a non-integer value."""
    raw = config.require(setting)
    try:
        return int(raw)
    except ValueError:
        raise CategorizedError(
            f"{setting.name}={raw!r} is not an integer",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None


class FaultInjectDiscovery:
    """The realized discovery port for the synthetic fault-inject resource."""

    def __init__(
        self,
        *,
        host_uri: str,
        concurrent_allocation_cap: int,
        seed: int,
        fault_rate: dict[str, float],
        max_latency_s: dict[str, float],
        secret_ref: str,
    ) -> None:
        self.host_uri = host_uri
        self.concurrent_allocation_cap = concurrent_allocation_cap
        self.seed = seed
        self.fault_rate = fault_rate
        self.max_latency_s = max_latency_s
        self.secret_ref = secret_ref

    @classmethod
    def from_env(cls) -> FaultInjectDiscovery:
        """Build from ``KDIVE_FAULT_INJECT_*`` env; happy-path defaults draw no fault.

        The per-plane ``fault_rate``/``max_latency_s`` maps default empty for the inert
        happy path; the seeded engine owns their configuration.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the cap or seed env var is not an
                integer.
        """
        return cls(
            host_uri=config.require(FAULT_INJECT_URI),
            concurrent_allocation_cap=_int_setting(FAULT_INJECT_ALLOCATION_CAP),
            seed=_int_setting(FAULT_INJECT_SEED),
            fault_rate={},
            max_latency_s={},
            secret_ref=config.require(FAULT_INJECT_SECRET_REF),
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one `ResourceRecord` for the synthetic fault-inject resource."""
        capabilities: dict[str, Any] = {
            "arch": "synthetic",
            "transports": ["gdbstub"],
            CONCURRENT_ALLOCATION_CAP_KEY: self.concurrent_allocation_cap,
            SEED_KEY: self.seed,
            FAULT_RATE_KEY: dict(self.fault_rate),
            MAX_LATENCY_S_KEY: dict(self.max_latency_s),
            SECRET_REF_KEY: self.secret_ref,
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.FAULT_INJECT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]
