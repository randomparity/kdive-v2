"""DB-backed `resolve_coeff` tests — fail-closed coefficient lookup (ADR-0007 §1)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import psycopg

from kdive.domain.cost import resolve_coeff
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_resolve_coeff_reads_seeded_local(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            coeff = await resolve_coeff(conn, "local")
        assert coeff == Decimal("1.0")

    asyncio.run(_run())


def test_resolve_coeff_missing_row_fails_closed(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            try:
                await resolve_coeff(conn, "cloud-standard")
                raise AssertionError("expected CategorizedError")
            except CategorizedError as exc:
                assert exc.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())
