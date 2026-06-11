"""The server dependency-set health checks (ADR-0090 §5): Postgres + MinIO + OIDC.

The checks are built from injected resources so the dependency set is a parameter, not
hardcoded — issue #267 reuses the same primitives with a different set (PG + MinIO, no
OIDC). Each check passes when its probe returns and reads as down when it raises.
"""

from __future__ import annotations

import asyncio

from kdive.health import HealthProbe
from kdive.health.server_checks import build_server_checks


class _FakeStore:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.pings = 0

    def ping(self) -> None:
        self.pings += 1
        if not self._ok:
            raise RuntimeError("minio down")


async def _pg_ok() -> None:
    return None


async def _pg_down() -> None:
    raise RuntimeError("pg down")


async def _oidc_ok() -> None:
    return None


async def _oidc_down() -> None:
    raise RuntimeError("idp down")


def test_server_set_is_postgres_minio_oidc() -> None:
    checks = build_server_checks(
        postgres_ping=_pg_ok, object_store_factory=lambda: _FakeStore(ok=True), oidc_ping=_oidc_ok
    )
    assert {c.name for c in checks} == {"postgres", "minio", "oidc"}


def test_store_factory_failure_reads_as_not_ready_not_crash() -> None:
    async def _run() -> None:
        def boom() -> _FakeStore:
            raise RuntimeError("KDIVE_S3_ENDPOINT_URL is not set")

        checks = build_server_checks(
            postgres_ping=_pg_ok, object_store_factory=boom, oidc_ping=_oidc_ok
        )
        result = await HealthProbe(checks=checks).check()
        assert result.ready is False
        assert result.checks["minio"] is False

    asyncio.run(_run())


def test_ready_when_all_up() -> None:
    async def _run() -> None:
        checks = build_server_checks(
            postgres_ping=_pg_ok,
            object_store_factory=lambda: _FakeStore(ok=True),
            oidc_ping=_oidc_ok,
        )
        assert (await HealthProbe(checks=checks).check()).ready is True

    asyncio.run(_run())


def test_not_ready_when_minio_down() -> None:
    async def _run() -> None:
        checks = build_server_checks(
            postgres_ping=_pg_ok,
            object_store_factory=lambda: _FakeStore(ok=False),
            oidc_ping=_oidc_ok,
        )
        result = await HealthProbe(checks=checks).check()
        assert result.ready is False
        assert result.checks["minio"] is False

    asyncio.run(_run())


def test_not_ready_when_oidc_down() -> None:
    async def _run() -> None:
        checks = build_server_checks(
            postgres_ping=_pg_ok,
            object_store_factory=lambda: _FakeStore(ok=True),
            oidc_ping=_oidc_down,
        )
        result = await HealthProbe(checks=checks).check()
        assert result.ready is False
        assert result.checks["oidc"] is False

    asyncio.run(_run())
