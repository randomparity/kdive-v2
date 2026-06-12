"""Remote-libvirt guest-agent readiness polling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.xml import agent_channel_connected

type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


class Domain(Protocol):
    """The domain slice readiness polling uses."""

    def isActive(self) -> int: ...  # noqa: N802
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802


class ReadinessConn(Protocol):
    """The connection slice readiness polling uses."""

    def lookupByName(self, name: str) -> Domain: ...  # noqa: N802


def wait_for_agent(
    conn: ReadinessConn,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
) -> None:
    """Poll the live XML until the guest-agent channel reports connected."""
    deadline = monotonic() + timeout_s
    while True:
        try:
            domain = conn.lookupByName(domain_name)
            running = bool(domain.isActive())
            connected = running and agent_channel_connected(domain.XMLDesc())
        except libvirt.libvirtError as exc:
            raise _infra("polling the guest-agent channel", domain=domain_name) from exc
        if connected:
            return
        if not running:
            raise CategorizedError(
                "domain exited during boot before the guest agent connected",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain_name},
            )
        if monotonic() >= deadline:
            raise CategorizedError(
                f"guest agent did not connect within {timeout_s:g}s",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain_name, "timeout_s": timeout_s},
            )
        sleep(poll_s)


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
