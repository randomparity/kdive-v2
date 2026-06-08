"""Unit tests for the per-kind ProviderResolver (ADR-0071)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.resolver import ProviderResolver


class _Runtime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.registered: list[object] = []

    async def register_discovery(self, pool: object) -> None:
        self.registered.append(pool)


def _resolver(*kinds: ResourceKind) -> tuple[ProviderResolver, dict[ResourceKind, _Runtime]]:
    runtimes = {k: _Runtime(k.value) for k in kinds}
    return ProviderResolver(cast(dict, runtimes)), runtimes


def test_resolve_returns_the_registered_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT) is runtimes[ResourceKind.LOCAL_LIBVIRT]


def test_resolve_unknown_kind_fails_closed_with_configuration_error() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.FAULT_INJECT)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "fault-inject" in str(exc.value)


def test_registered_kinds_reflects_the_map() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})


def test_empty_resolver_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ProviderResolver({})


def test_register_all_discovery_fans_out_over_every_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    pool = cast(AsyncConnectionPool, object())
    asyncio.run(resolver.register_all_discovery(pool))
    assert runtimes[ResourceKind.LOCAL_LIBVIRT].registered == [pool]
