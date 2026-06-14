"""Worker/reconciler-process readiness probe builders (ADR-0090 §5).

Binds the generic, dependency-set-agnostic :mod:`kdive.health` primitives to the
worker's and reconciler's concrete backends: a Postgres ``SELECT 1`` over the shared pool
and a MinIO bucket ``HEAD`` — **no OIDC** (they pull jobs from the DB and never verify
tokens, so their readiness must not couple to the IdP). Kept out of :mod:`kdive.health`
so that package stays free of server-stack imports, mirroring
:mod:`kdive.process_health.server`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from kdive.health.probe import HealthProbe
from kdive.health.worker_checks import build_worker_checks


class _Pingable(Protocol):
    def ping(self) -> None: ...


def build_worker_probe(
    *,
    postgres_ping: Callable[[], Awaitable[None]],
    object_store_factory: Callable[[], _Pingable],
) -> HealthProbe:
    """Return the worker/reconciler readiness probe (Postgres + MinIO, no OIDC).

    Args:
        postgres_ping: Coroutine running a ``SELECT 1`` over the shared pool.
        object_store_factory: Builds an object store exposing a synchronous ``ping()``;
            called inside the check so a misconfigured store reads as not-ready.
    """
    return HealthProbe(
        checks=build_worker_checks(
            postgres_ping=postgres_ping,
            object_store_factory=object_store_factory,
        )
    )
