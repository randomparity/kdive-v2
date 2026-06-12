"""Compose the **worker/reconciler** readiness set (ADR-0090 §5): PG + MinIO, no OIDC.

The dependency set is assembled from injected resources, mirroring
:mod:`kdive.health.server_checks` minus the OIDC check: the worker and reconciler pull
jobs from the DB and never verify tokens, so their readiness must **not** couple to the
IdP. The Postgres probe is passed in as a coroutine built by the process entrypoint (it
owns the pool); the object-store ping is offloaded to a thread because the boto3 client
is synchronous, and the store is built **inside the check** so a misconfigured or
unreachable store reads as not-ready rather than crashing process startup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from kdive.health.probe import BackendCheck


class _Pingable(Protocol):
    def ping(self) -> None: ...


def build_worker_checks(
    *,
    postgres_ping: Callable[[], Awaitable[None]],
    object_store_factory: Callable[[], _Pingable],
) -> list[BackendCheck]:
    """Return the worker/reconciler readiness checks: Postgres, MinIO (no OIDC).

    Args:
        postgres_ping: Coroutine running a ``SELECT 1`` over the shared pool.
        object_store_factory: Builds an object store exposing a synchronous ``ping()``
            (bucket ``HEAD``). Called **inside the check**, so a misconfigured or
            unreachable store reads as not-ready rather than crashing process startup.
    """

    async def minio() -> None:
        await asyncio.to_thread(_ping_store, object_store_factory)

    return [
        BackendCheck(name="postgres", probe=postgres_ping),
        BackendCheck(name="minio", probe=minio),
    ]


def _ping_store(factory: Callable[[], _Pingable]) -> None:
    factory().ping()
