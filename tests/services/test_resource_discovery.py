"""Tests for Resource discovery registration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.discovery import ResourceRecord
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.services.resources.discovery import (
    ensure_discovered_resource_registered,
    register_discovered_resource,
)


class _Discovery:
    def __init__(self, cap: int = 2) -> None:
        self.cap = cap
        self.calls = 0

    def list_resources(self) -> list[ResourceRecord]:
        self.calls += 1
        return [
            ResourceRecord(
                resource_id="qemu:///system",
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={
                    "arch": "x86_64",
                    "vcpus": 8,
                    "memory_mb": 16384,
                    "transports": ["gdbstub"],
                    CONCURRENT_ALLOCATION_CAP_KEY: self.cap,
                },
                status=ResourceStatus.AVAILABLE,
            )
        ]


@asynccontextmanager
async def _pg(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


def test_register_discovered_resource_is_idempotent(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pg(migrated_url) as conn:
            first = await register_discovered_resource(
                conn,
                _Discovery(cap=2).list_resources()[0],
                pool="local-libvirt",
                cost_class="local",
            )
            second = await register_discovered_resource(
                conn,
                _Discovery(cap=5).list_resources()[0],
                pool="local-libvirt",
                cost_class="local",
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM resources")
                row = await cur.fetchone()
        assert first.host_uri == "qemu:///system"
        assert first.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 2
        assert second.id == first.id
        assert second.capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 5
        assert row is not None and row[0] == 1

    asyncio.run(_run())


def test_ensure_discovered_resource_registered_bootstraps_one_row(migrated_url: str) -> None:
    async def _run() -> None:
        discovery = _Discovery(cap=2)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, discovery)
            await _ensure(pool, discovery)
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT kind, host_uri FROM resources")
            rows = await cur.fetchall()
        assert rows == [("local-libvirt", "qemu:///system")]
        assert discovery.calls == 1

    asyncio.run(_run())


def test_ensure_discovered_resource_registered_does_not_overwrite_existing_row(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(cap=2))
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET capabilities = "
                    "jsonb_set(capabilities, '{concurrent_allocation_cap}', '9'::jsonb)"
                )
                await conn.commit()
            await _ensure(pool, _Discovery(cap=1))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT capabilities->>'concurrent_allocation_cap' FROM resources")
            row = await cur.fetchone()
        assert row is not None and row[0] == "9"

    asyncio.run(_run())


async def _ensure(pool: AsyncConnectionPool, discovery: _Discovery) -> None:
    await ensure_discovered_resource_registered(
        pool,
        discovery,
        kind=ResourceKind.LOCAL_LIBVIRT,
        resource_id="qemu:///system",
        pool_name="local-libvirt",
        cost_class="local",
    )
