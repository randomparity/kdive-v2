"""Shared MCP provider-runtime resolution tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._runtime_resolution import (
    RuntimeHandler,
    with_runtime_for_allocation,
    with_runtime_for_run,
    with_runtime_for_system,
)
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime

type _RuntimeWrapper = Callable[
    [AsyncConnectionPool, ProviderResolver, str, RuntimeHandler],
    Coroutine[Any, Any, ToolResponse],
]

_OBJECT_ID = "11111111-1111-1111-1111-111111111111"
_RUNTIME = cast(ProviderRuntime, object())
_WRAPPERS: tuple[tuple[str, _RuntimeWrapper], ...] = (
    ("allocation", with_runtime_for_allocation),
    ("system", with_runtime_for_system),
    ("run", with_runtime_for_run),
)


class _FakeConn:
    pass


class _ConnectionContext:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()
        self.connections = 0

    def connection(self) -> _ConnectionContext:
        self.connections += 1
        return _ConnectionContext(self.conn)


class _FakeResolver:
    def __init__(self, *, error: CategorizedError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, _FakeConn, UUID]] = []

    async def runtime_for_allocation(self, conn: _FakeConn, uid: UUID) -> ProviderRuntime:
        return self._runtime_for("allocation", conn, uid)

    async def runtime_for_system(self, conn: _FakeConn, uid: UUID) -> ProviderRuntime:
        return self._runtime_for("system", conn, uid)

    async def runtime_for_run(self, conn: _FakeConn, uid: UUID) -> ProviderRuntime:
        return self._runtime_for("run", conn, uid)

    def _runtime_for(self, kind: str, conn: _FakeConn, uid: UUID) -> ProviderRuntime:
        self.calls.append((kind, conn, uid))
        if self.error is not None:
            raise self.error
        return _RUNTIME


def _pool(pool: _FakePool) -> AsyncConnectionPool:
    return cast(AsyncConnectionPool, pool)


def _resolver(resolver: _FakeResolver) -> ProviderResolver:
    return cast(ProviderResolver, resolver)


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_maps_malformed_id_to_failure_response(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    del kind
    pool = _FakePool()
    resolver = _FakeResolver()

    result = asyncio.run(wrapper(_pool(pool), _resolver(resolver), "not-a-uuid", _success_response))

    assert result.object_id == "not-a-uuid"
    assert result.status == "error"
    assert result.error_category == "configuration_error"
    assert pool.connections == 0
    assert resolver.calls == []


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_maps_categorized_error_to_failure_response(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    del kind
    error = CategorizedError(
        "runtime unavailable",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={
            "resource_kind": "local_libvirt",
            "retryable": False,
            "nested": {"not": "surfaced"},
        },
    )
    pool = _FakePool()
    resolver = _FakeResolver(error=error)

    result = asyncio.run(wrapper(_pool(pool), _resolver(resolver), _OBJECT_ID, _success_response))

    assert result.object_id == _OBJECT_ID
    assert result.status == "error"
    assert result.error_category == "missing_dependency"
    assert result.data == {"resource_kind": "local_libvirt", "retryable": False}
    assert pool.connections == 1


async def _success_response(runtime: ProviderRuntime) -> ToolResponse:
    assert runtime is _RUNTIME
    return ToolResponse.success(_OBJECT_ID, "succeeded")
