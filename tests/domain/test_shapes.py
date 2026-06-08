"""DB-backed `resolve_shape` tests — fail-closed shape lookup (ADR-0067)."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.shapes import ShapeSizing, resolve_shape

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
