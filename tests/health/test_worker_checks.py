"""The worker/reconciler dependency-set health checks (ADR-0090 §5): PG + MinIO, no OIDC.

The worker and reconciler pull jobs from the DB and never verify tokens, so their
readiness must **not** couple to the IdP. This composer reuses the same
:class:`~kdive.health.HealthProbe` primitives as the server set, minus the OIDC check.
"""

from __future__ import annotations

import asyncio

from kdive.health.probe import HealthProbe
from kdive.health.worker_checks import build_worker_checks


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


def test_worker_set_is_postgres_and_minio_only() -> None:
    checks = build_worker_checks(
        postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
    )
    assert {c.name for c in checks} == {"postgres", "minio"}


def test_worker_set_omits_oidc() -> None:
    checks = build_worker_checks(
        postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
    )
    assert "oidc" not in {c.name for c in checks}


def test_ready_when_pg_and_minio_up() -> None:
    async def _run() -> None:
        checks = build_worker_checks(
            postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True)
        )
        assert (await HealthProbe(checks=checks).check()).ready is True

    asyncio.run(_run())


def test_not_ready_when_pg_down() -> None:
    async def _run() -> None:
        checks = build_worker_checks(
            postgres_ping=_pg_down, object_store_factory=lambda: _FakeStore(ok=True)
        )
        result = await HealthProbe(checks=checks).check()
        assert result.ready is False
        assert result.checks["postgres"] is False

    asyncio.run(_run())


def test_minio_factory_failure_reads_as_not_ready() -> None:
    async def _run() -> None:
        def boom() -> _FakeStore:
            raise RuntimeError("KDIVE_S3_ENDPOINT_URL is not set")

        checks = build_worker_checks(postgres_ping=_pg_ok, object_store_factory=boom)
        result = await HealthProbe(checks=checks).check()
        assert result.ready is False
        assert result.checks["minio"] is False

    asyncio.run(_run())
