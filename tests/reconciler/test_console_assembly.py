"""Assembly tests for reconciler-owned remote console hosting."""

from __future__ import annotations

import asyncio

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt import composition as remote_composition
from kdive.reconciler import console_assembly
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeLeaderConn:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakePool:
    def __init__(self) -> None:
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


def test_build_console_hosting_returns_none_when_remote_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        monkeypatch.setattr(remote_composition, "database_url", lambda: "postgresql://db/kdive")
        monkeypatch.setattr(remote_composition, "object_store_from_env", lambda: object())

        def _missing_config() -> object:
            raise CategorizedError(
                "remote config missing", category=ErrorCategory.CONFIGURATION_ERROR
            )

        monkeypatch.setattr(remote_composition, "remote_config_from_env", _missing_config)

        hosting = await remote_composition.build_console_hosting(secret_registry=SecretRegistry())
        assert hosting is None

    asyncio.run(_run())


def test_build_console_hosting_opens_host_pool_and_returns_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        leader_conn = _FakeLeaderConn()
        host_pool = _FakePool()
        monkeypatch.setattr(remote_composition, "database_url", lambda: "postgresql://db/kdive")
        monkeypatch.setattr(remote_composition, "object_store_from_env", lambda: object())
        monkeypatch.setattr(remote_composition, "remote_config_from_env", lambda: object())
        monkeypatch.setattr(remote_composition, "secret_backend_from_env", lambda **_: object())
        monkeypatch.setattr(remote_composition, "create_pool", lambda **_: host_pool)

        async def _connect(conninfo: str, *, autocommit: bool) -> _FakeLeaderConn:
            assert conninfo == "postgresql://db/kdive"
            assert autocommit is True
            return leader_conn

        monkeypatch.setattr(
            remote_composition.psycopg.AsyncConnection, "connect", staticmethod(_connect)
        )

        hosting = await remote_composition.build_console_hosting(secret_registry=SecretRegistry())

        assert hosting is not None
        assert hosting.registry is not None
        assert host_pool.opened is True

    asyncio.run(_run())


def test_start_console_hosting_none_returns_none() -> None:
    assert console_assembly.start_console_hosting(None, asyncio.Event()) is None


def test_console_hosting_close_closes_leader_and_host_pool() -> None:
    async def _run() -> None:
        leader_conn = _FakeLeaderConn()
        host_pool = _FakePool()
        hosting = console_assembly.ConsoleHosting(
            loop=object(),  # ty: ignore[invalid-argument-type]
            registry=object(),  # ty: ignore[invalid-argument-type]
            leader_conn=leader_conn,  # ty: ignore[invalid-argument-type]
            host_pool=host_pool,  # ty: ignore[invalid-argument-type]
        )

        await hosting.close()

        assert leader_conn.closed is True
        assert host_pool.closed is True

    asyncio.run(_run())
