"""Fault-inject Discovery plane (ADR-0071, ADR-0072).

`FaultInjectDiscovery` advertises one synthetic resource row — there is no host to
enumerate, so the "discovered" resource is the mock itself. Its ``capabilities`` jsonb
carries the per-plane concurrent-allocation cap plus the fault-engine keys (``seed`` /
``fault_rate`` / ``max_latency_s`` / ``secret_ref``); the seeded engine (issue 3) and
forced secret resolution (issue 4) read those keys. Happy-path discovery writes an empty
``fault_rate``/``max_latency_s`` so no fault is drawn until a deployment configures one.
"""

from __future__ import annotations

import os
from typing import Any

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

_URI_ENV = "KDIVE_FAULT_INJECT_URI"
_CAP_ENV = "KDIVE_FAULT_INJECT_ALLOCATION_CAP"
_SEED_ENV = "KDIVE_FAULT_INJECT_SEED"
_SECRET_REF_ENV = "KDIVE_FAULT_INJECT_SECRET_REF"  # pragma: allowlist secret - env var name

_DEFAULT_URI = "fault-inject://local"
_DEFAULT_CAP = 1
_DEFAULT_SEED = 0
_DEFAULT_SECRET_REF = "fault-inject/console-sentinel"  # pragma: allowlist secret - ref, not a value


def _int_env(name: str, default: int) -> int:
    """Read an integer env var, raising ``CONFIGURATION_ERROR`` on a non-integer value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise CategorizedError(
            f"{name}={raw!r} is not an integer",
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

        The per-plane ``fault_rate``/``max_latency_s`` maps default empty — the seeded
        engine (issue 3) owns their configuration; issue 2 ships the inert happy path.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the cap or seed env var is not an
                integer.
        """
        return cls(
            host_uri=os.environ.get(_URI_ENV, _DEFAULT_URI),
            concurrent_allocation_cap=_int_env(_CAP_ENV, _DEFAULT_CAP),
            seed=_int_env(_SEED_ENV, _DEFAULT_SEED),
            fault_rate={},
            max_latency_s={},
            secret_ref=os.environ.get(_SECRET_REF_ENV, _DEFAULT_SECRET_REF),
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
