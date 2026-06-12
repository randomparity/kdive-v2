"""Tests for server-process readiness probe builders."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.process_health import server


def test_oidc_ping_requires_jwks_uri() -> None:
    try:
        config.load({})
        with pytest.raises(CategorizedError) as exc:
            server.build_oidc_ping()
        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.value.details["variable"] == "KDIVE_OIDC_JWKS_URI"
    finally:
        config.reset()


def test_oidc_ping_uses_bounded_client_and_raises_for_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        try:
            config.load({"KDIVE_OIDC_JWKS_URI": "https://issuer.example/jwks"})
            calls: list[object] = []

            class _Client:
                def __init__(self, *, timeout: float) -> None:
                    calls.append(("timeout", timeout))

                async def __aenter__(self) -> _Client:
                    return self

                async def __aexit__(self, *exc: object) -> None:
                    return None

                async def get(self, url: str) -> httpx.Response:
                    calls.append(("get", url))
                    return httpx.Response(
                        503,
                        request=httpx.Request("GET", url),
                    )

            monkeypatch.setattr(server.httpx, "AsyncClient", _Client)
            ping = server.build_oidc_ping()

            with pytest.raises(httpx.HTTPStatusError):
                await ping()

            assert calls == [
                ("timeout", server._OIDC_PROBE_TIMEOUT),
                ("get", "https://issuer.example/jwks"),
            ]
        finally:
            config.reset()

    asyncio.run(_run())


def test_postgres_ping_executes_select_one() -> None:
    async def _run() -> None:
        cursor = _FakeCursor()
        pool = _FakePool(cursor)

        ping = server.build_postgres_ping(cast(AsyncConnectionPool, pool))
        await ping()

        assert cursor.executed == ["SELECT 1"]
        assert cursor.fetched is True

    asyncio.run(_run())


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.fetched = False

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def fetchone(self) -> tuple[int]:
        self.fetched = True
        return (1,)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _FakePool:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def connection(self) -> Any:
        return _FakeConnection(self._cursor)
