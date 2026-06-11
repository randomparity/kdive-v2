"""Worker/reconciler readiness probe wiring (ADR-0090 §5).

Binds the dependency-set-agnostic :mod:`kdive.health` primitives to the worker's and
reconciler's concrete backends — Postgres + MinIO, **no OIDC** — kept out of
:mod:`kdive.health` so that package stays free of server-stack imports.
"""

from __future__ import annotations

import asyncio

from kdive.health import HealthProbe
from kdive.worker_health import build_worker_probe


class _FakeStore:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok

    def ping(self) -> None:
        if not self._ok:
            raise RuntimeError("minio down")


async def _pg_ok() -> None:
    return None


async def _pg_down() -> None:
    raise RuntimeError("pg down")


def test_probe_ready_when_backends_up() -> None:
    async def _run() -> None:
        probe = build_worker_probe(
            postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
        )
        assert isinstance(probe, HealthProbe)
        assert (await probe.check()).ready is True

    asyncio.run(_run())


def test_probe_not_ready_when_pg_down() -> None:
    async def _run() -> None:
        probe = build_worker_probe(
            postgres_ping=_pg_down, object_store_factory=lambda: _FakeStore(ok=True)
        )
        result = await probe.check()
        assert result.ready is False
        assert result.checks["postgres"] is False

    asyncio.run(_run())


def test_probe_does_not_couple_to_oidc() -> None:
    async def _run() -> None:
        probe = build_worker_probe(
            postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
        )
        result = await probe.check()
        assert "oidc" not in result.checks

    asyncio.run(_run())


def test_readyz_over_aux_listener_omits_oidc() -> None:
    """End-to-end: the worker /readyz body carries postgres + minio, never oidc."""

    async def _run() -> None:
        import httpx

        from kdive.health import Heartbeat
        from kdive.health.aux_listener import build_aux_app

        probe = build_worker_probe(
            postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
        )
        app = build_aux_app(heartbeat=Heartbeat(stale_after=1e9), probe=probe, metric_reader=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aux") as client:
            resp = await client.get("/readyz")
            assert resp.status_code == 200
            checks = resp.json()["checks"]
            assert set(checks) == {"postgres", "minio"}
            assert "oidc" not in checks

    asyncio.run(_run())
