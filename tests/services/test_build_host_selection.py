"""Build-host selection policy tests."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.services.runs import build_host_selection

_RUN_ID = UUID("00000000-0000-0000-0000-00000000b017")


class _Profile:
    def __init__(self, build_host: str | None = None) -> None:
        self.build_host = build_host


def _host(
    *,
    name: str = "worker-local",
    kind: BuildHostKind = BuildHostKind.LOCAL,
    enabled: bool = True,
    state: BuildHostState = BuildHostState.READY,
) -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-00000000b018"),
        name=name,
        kind=kind,
        address="builder.example" if kind is not BuildHostKind.LOCAL else None,
        ssh_credential_ref="ssh://builder" if kind is BuildHostKind.SSH else None,
        base_image_volume="base.qcow2" if kind is BuildHostKind.EPHEMERAL_LIBVIRT else None,
        workspace_root="/build",
        max_concurrent=1,
        enabled=enabled,
        state=state,
    )


def test_local_host_default_does_not_acquire_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host()
        acquired: list[BuildHost] = []
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))
        monkeypatch.setattr(build_host_selection, "is_git_source", lambda _profile: False)
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, lease_host, _run_id: _record_async(acquired, lease_host, True),
        )

        selected = await build_host_selection.resolve_and_admit(
            cast(AsyncConnection, object()), cast(ServerBuildProfile, _Profile()), _RUN_ID
        )

        assert selected is host
        assert acquired == []

    asyncio.run(_run())


def test_remote_host_requires_git_source_and_acquires_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH)
        acquired: list[BuildHost] = []
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))
        monkeypatch.setattr(build_host_selection, "is_git_source", lambda _profile: True)
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, lease_host, _run_id: _record_async(acquired, lease_host, True),
        )

        selected = await build_host_selection.resolve_and_admit(
            cast(AsyncConnection, object()),
            cast(ServerBuildProfile, _Profile("builder")),
            _RUN_ID,
        )

        assert selected is host
        assert acquired == [host]

    asyncio.run(_run())


def test_missing_host_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(None))

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                cast(ServerBuildProfile, _Profile("missing")),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.NOT_FOUND
        assert exc.value.details == {"build_host": "missing"}

    asyncio.run(_run())


def test_remote_host_at_capacity_is_capacity_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        host = _host(name="builder", kind=BuildHostKind.SSH)
        monkeypatch.setattr(build_host_selection, "get_by_name", lambda _conn, name: _async(host))
        monkeypatch.setattr(build_host_selection, "is_git_source", lambda _profile: True)
        monkeypatch.setattr(
            build_host_selection,
            "try_acquire_lease",
            lambda _conn, _host, _run_id: _async(False),
        )

        with pytest.raises(CategorizedError) as exc:
            await build_host_selection.resolve_and_admit(
                cast(AsyncConnection, object()),
                cast(ServerBuildProfile, _Profile("builder")),
                _RUN_ID,
            )

        assert exc.value.category is ErrorCategory.CAPACITY_EXHAUSTED

    asyncio.run(_run())


async def _async[T](value: T) -> T:
    return value


async def _record_async[T](items: list[T], item: T, value: bool) -> bool:
    items.append(item)
    return value
