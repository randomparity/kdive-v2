"""Resolve the aux-listener bind address from config (ADR-0090 §5).

Splits the single ``KDIVE_HEALTH_BIND_ADDR`` ``host:port`` key into a host/port pair for
uvicorn. Kept separate from :mod:`kdive.health.aux_listener` so the parse is unit-tested
without importing the server stack, and so the config-contract trust boundary (loopback
default) is enforced in one place.
"""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import HEALTH_BIND_ADDR
from kdive.domain.errors import CategorizedError, ErrorCategory


def resolve_health_bind() -> tuple[str, int]:
    """Return the aux listener ``(host, port)`` from ``KDIVE_HEALTH_BIND_ADDR``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the value is not a ``host:port``
            with an integer port.
    """
    raw = config.require(HEALTH_BIND_ADDR)
    host, sep, port_text = raw.rpartition(":")
    if not sep or not host:
        raise _bad(raw)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise _bad(raw) from exc
    return host, port


def _bad(raw: str) -> CategorizedError:
    return CategorizedError(
        f"{HEALTH_BIND_ADDR.name}: {raw!r} is not a host:port",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"variable": HEALTH_BIND_ADDR.name, "suggest": HEALTH_BIND_ADDR.suggest},
    )
