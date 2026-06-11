"""Shared service-health surface for all three kdive processes (ADR-0090 §5).

This package supplies the building blocks the ``server`` (this issue) and the
``worker``/``reconciler`` (issue #267) share:

- :class:`BackendCheck` / :class:`HealthProbe` — the readiness probe whose **dependency
  set is injected**, with the caching asymmetry (healthy-cached, failure-immediate) and
  a per-check timeout;
- :class:`Heartbeat` — the affirmative ``/livez`` loop-tick signal (tracks the loop, not
  the work unit);
- :func:`build_aux_app` / :func:`serve_aux` — the dedicated auxiliary HTTP listener
  exposing ``/livez``, ``/readyz``, ``/metrics``, **distinct** from the server's public
  MCP port and **bound loopback by default** (the bind address is a config key, so the
  trust boundary is the config contract, not implementation memory).
"""

from __future__ import annotations

from kdive.health.aux_listener import build_aux_app, serve_aux
from kdive.health.heartbeat import Heartbeat
from kdive.health.probe import BackendCheck, HealthProbe, ReadyResult

__all__ = [
    "BackendCheck",
    "Heartbeat",
    "HealthProbe",
    "ReadyResult",
    "build_aux_app",
    "serve_aux",
]
