"""Runtime holder for reconciler-owned console hosting."""

from __future__ import annotations

import asyncio
import contextlib

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.reconciler.console_hosting import (
    CollectorRegistry,
    ConsoleHostingLoop,
)

CONSOLE_ATTACH_TICK_SECONDS = 1.0


class ConsoleHosting:
    """Holds the console hosting loop, registry, and dedicated leader connection."""

    def __init__(
        self,
        loop: ConsoleHostingLoop,
        registry: CollectorRegistry,
        leader_conn: psycopg.AsyncConnection,
        host_pool: AsyncConnectionPool,
    ) -> None:
        self.loop = loop
        self.registry = registry
        self._leader_conn = leader_conn
        self._host_pool = host_pool

    async def run(self, stop: asyncio.Event) -> None:
        """Run the attach watcher until ``stop`` is set."""
        try:
            while not stop.is_set():
                await self.loop.tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=CONSOLE_ATTACH_TICK_SECONDS)
        finally:
            await self.loop.stop()

    async def close(self) -> None:
        """Release the leader connection and host pool."""
        await self._leader_conn.close()
        await self._host_pool.close()


def start_console_hosting(
    console_hosting: ConsoleHosting | None, stop: asyncio.Event
) -> asyncio.Task[None] | None:
    """Start the hosting loop concurrently with ``Reconciler.run``."""
    if console_hosting is None:
        return None
    return asyncio.create_task(console_hosting.run(stop))
