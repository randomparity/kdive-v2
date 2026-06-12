"""Assembly for reconciler-owned remote console hosting."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import CONSOLE_HOSTING_LEADER, SessionAdvisoryLock
from kdive.db.pool import create_pool, database_url
from kdive.domain.errors import CategorizedError
from kdive.providers.remote_libvirt.config import remote_config_from_env
from kdive.providers.remote_libvirt.console.collector import ConsoleCollector
from kdive.providers.remote_libvirt.console.wiring import (
    RemoteConsolePartStore,
    open_remote_console,
)
from kdive.reconciler.console_hosting import (
    AsyncioPumpRunner,
    CollectorRegistry,
    ConsoleHostingLoop,
    DbRunningRemoteSystems,
)
from kdive.security.secrets.secrets import secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

if TYPE_CHECKING:
    from kdive.security.secrets.secret_registry import SecretRegistry

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


async def build_console_hosting(secret_registry: SecretRegistry) -> ConsoleHosting | None:
    """Build the single-leader console hosting loop, or ``None`` when unconfigured."""
    try:
        conninfo = database_url()
        store = object_store_from_env()
        remote_config = remote_config_from_env()
        secret_backend = secret_backend_from_env(registry=secret_registry)
    except CategorizedError:
        return None

    part_store = RemoteConsolePartStore(store, conninfo)
    leader_conn = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
    lock = SessionAdvisoryLock(leader_conn, CONSOLE_HOSTING_LEADER)
    runner = AsyncioPumpRunner()
    registry = CollectorRegistry(pump_runner=runner)
    host_pool = create_pool(min_size=1)
    await host_pool.open()

    def factory(system_id: object) -> ConsoleCollector:
        from uuid import UUID

        assert isinstance(system_id, UUID)
        return ConsoleCollector(
            system_id,
            open_console=lambda sid: open_remote_console(remote_config, secret_backend, sid),
            store=part_store,
            secret_registry=secret_registry,
        )

    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=DbRunningRemoteSystems(host_pool),
        collector_factory=factory,
        registry=registry,
        pump_runner=runner,
    )
    return ConsoleHosting(loop, registry, leader_conn, host_pool)


def start_console_hosting(
    console_hosting: ConsoleHosting | None, stop: asyncio.Event
) -> asyncio.Task[None] | None:
    """Start the hosting loop concurrently with ``Reconciler.run``."""
    if console_hosting is None:
        return None
    return asyncio.create_task(console_hosting.run(stop))
