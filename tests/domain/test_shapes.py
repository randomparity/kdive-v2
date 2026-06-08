"""DB-backed `resolve_shape` tests — fail-closed shape lookup (ADR-0067)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.shapes import (
    ResolvedSizing,
    ShapeSizing,
    resolve_request_sizing,
    resolve_shape,
)

# The four shapes migration 0013 seeds (ADR-0067), with their sizing tuples.
_SEED_SHAPES = {
    "small": (1, 1024, 10),
    "medium": (2, 4096, 20),
    "large": (4, 8192, 40),
    "max": (8, 16384, 80),
}


@pytest.mark.parametrize("name", sorted(_SEED_SHAPES))
def test_resolve_shape_returns_seeded_tuple(migrated_url: str, name: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_shape(conn, name)
        vcpus, memory_mb, disk_gb = _SEED_SHAPES[name]
        assert sizing == ShapeSizing(
            vcpus=vcpus, memory_mb=memory_mb, disk_gb=disk_gb, pcie_match=None
        )

    asyncio.run(_run())


def test_resolve_shape_unknown_name_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            try:
                await resolve_shape(conn, "no-such-shape")
                raise AssertionError("expected CategorizedError")
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR
                assert exc.details["shape"] == "no-such-shape"

    asyncio.run(_run())


def test_shape_sizing_does_not_carry_cost_class() -> None:
    # A shape fixes size only; cost_class stays host-resolved (ADR-0067), so the resolved
    # tuple exposes no cost_class field a caller could mistake for one.
    assert "cost_class" not in ShapeSizing.model_fields
    assert set(ShapeSizing.model_fields) == {"vcpus", "memory_mb", "disk_gb", "pcie_match"}


def test_resolve_request_sizing_maps_shape_to_priced_tuple(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_request_sizing(
                conn, shape="large", vcpus=None, memory_gb=None, disk_gb=None
            )
        # large = 4 vcpu / 8192 MB / 40 GB; memory_mb -> memory_gb is lossless (// 1024).
        assert sizing == ResolvedSizing(
            vcpus=4, memory_gb=8, disk_gb=40, pcie_match=None, shape="large"
        )

    asyncio.run(_run())


def test_resolve_request_sizing_passes_custom_triple_through(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            sizing = await resolve_request_sizing(
                conn, shape=None, vcpus=3, memory_gb=6, disk_gb=30
            )
        assert sizing == ResolvedSizing(
            vcpus=3, memory_gb=6, disk_gb=30, pcie_match=None, shape=None
        )

    asyncio.run(_run())


def test_resolve_request_sizing_unknown_shape_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_request_sizing(
                    conn, shape="nope", vcpus=None, memory_gb=None, disk_gb=None
                )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_resolve_request_sizing_incomplete_custom_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as exc:
                await resolve_request_sizing(conn, shape=None, vcpus=3, memory_gb=6, disk_gb=None)
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())
