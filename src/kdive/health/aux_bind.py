"""Resolve the aux-listener bind address from config (ADR-0090 §5).

Splits the single ``KDIVE_HEALTH_BIND_ADDR`` ``host:port`` key into a host/port pair for
uvicorn. Kept separate from :mod:`kdive.health.aux_listener` so the parse is unit-tested
without importing the server stack, and so the config-contract trust boundary (loopback
default) is enforced in one place.

The bind address is a single validated config key, but the three processes share one host
(server, worker, reconciler) under ``docker compose`` and in a single-host operator run.
An explicit ``KDIVE_HEALTH_BIND_ADDR`` is always the source of truth and wins for every
process; only the **default** is specialized per process — server ``9464``, worker
``9465``, reconciler ``9466`` — so three default-configured processes on one host do not
collide on one port. Widening the boundary (e.g. ``0.0.0.0``) stays a single explicit,
reviewed config act that applies uniformly.
"""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import HEALTH_BIND_ADDR
from kdive.domain.errors import CategorizedError, ErrorCategory

#: Per-process default aux port offsets from the registered default host, so three
#: default-configured processes on one host bind distinct ports (ADR-0090 §5).
_PROCESS_DEFAULT_PORTS: dict[str, int] = {
    "server": 9464,
    "worker": 9465,
    "reconciler": 9466,
}


def resolve_health_bind(process: str = "server") -> tuple[str, int]:
    """Return the aux listener ``(host, port)`` for ``process``.

    An explicit ``KDIVE_HEALTH_BIND_ADDR`` wins for every process (the single
    source-of-truth contract). When unset, the default host is kept but the port is
    specialized per process so three default-configured processes on one host do not
    collide.

    Args:
        process: One of ``server``/``worker``/``reconciler``; selects the default port
            when ``KDIVE_HEALTH_BIND_ADDR`` is unset. An explicit value ignores it.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if an explicit value is not a
            ``host:port`` with an integer port.
    """
    explicit = HEALTH_BIND_ADDR.name in config.env_snapshot()
    raw = config.require(HEALTH_BIND_ADDR)
    host, sep, port_text = raw.rpartition(":")
    if not sep or not host:
        raise _bad(raw)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise _bad(raw) from exc
    if not explicit:
        port = _PROCESS_DEFAULT_PORTS.get(process, port)
    return host, port


def _bad(raw: str) -> CategorizedError:
    return CategorizedError(
        f"{HEALTH_BIND_ADDR.name}: {raw!r} is not a host:port",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"variable": HEALTH_BIND_ADDR.name, "suggest": HEALTH_BIND_ADDR.suggest},
    )
