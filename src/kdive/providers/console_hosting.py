"""Provider-neutral console-collector hosting runtime (ADR-0095)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ResourceKind
from kdive.domain.state import SystemState

_log = logging.getLogger(__name__)

CONSOLE_ATTACH_TICK_SECONDS = 1.0
_IDLE_PUMP_BACKOFF_SECONDS = 0.5

# A remote System in one of these states has a live domain whose console should be streamed.
# Terminal states (torn_down, failed) and pre-domain states (defined, provisioning) are excluded.
_RUNNING_SYSTEM_STATE_VALUES = (
    SystemState.READY.value,
    SystemState.REPROVISIONING.value,
    SystemState.CRASHED.value,
)
_REMOTE_KIND_VALUE = ResourceKind.REMOTE_LIBVIRT.value


class Collector(Protocol):
    """The structural collector contract the registry and hosting loop drive."""

    @property
    def system_id(self) -> UUID: ...
    def pump_once(self) -> bool: ...
    def finalize(self) -> None: ...
    def close(self) -> None: ...


class LeaderLock(Protocol):
    """The session-scoped leadership claim the hosting loop gates on."""

    async def try_acquire(self) -> bool: ...
    async def is_held(self) -> bool: ...
    async def release(self) -> None: ...


class RunningSystems(Protocol):
    """Reports Systems that should have a live console collector."""

    async def list_running(self) -> set[UUID]: ...


class DbRunningRemoteSystems:
    """Production :class:`RunningSystems`: the running remote Systems from Postgres.

    Selects remote-libvirt Systems with a live domain on a fresh pooled connection per call.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_running(self) -> set[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT s.id FROM systems s "
                "JOIN allocations a ON a.id = s.allocation_id "
                "JOIN resources r ON r.id = a.resource_id "
                "WHERE r.kind = %s AND s.state = ANY(%s) AND s.domain_name IS NOT NULL",
                (_REMOTE_KIND_VALUE, list(_RUNNING_SYSTEM_STATE_VALUES)),
            )
            return {row[0] for row in await cur.fetchall()}


class CollectorFactory(Protocol):
    """Builds a per-System collector."""

    def __call__(self, system_id: UUID, /) -> Collector: ...


class PumpRunner(Protocol):
    """Drives a collector's continuous pumping off the event loop."""

    def start(self, collector: Collector) -> None: ...
    def cancel(self, system_id: UUID) -> None: ...
    def cancel_all(self) -> None: ...


class CollectorRegistry:
    """The live per-System collectors, shared by hosting and reap logic."""

    def __init__(self, pump_runner: PumpRunner | None = None) -> None:
        self._collectors: dict[UUID, Collector] = {}
        self._pump_runner = pump_runner

    def has(self, system_id: UUID) -> bool:
        return system_id in self._collectors

    def system_ids(self) -> set[UUID]:
        return set(self._collectors)

    def get(self, system_id: UUID) -> Collector | None:
        return self._collectors.get(system_id)

    def add(self, collector: Collector) -> None:
        self._collectors[collector.system_id] = collector

    def drop(self, system_id: UUID) -> None:
        """Close and forget a collector without finalizing."""
        collector = self._collectors.pop(system_id, None)
        if collector is not None:
            self._cancel_pump(system_id)
            collector.close()

    def finalize_and_drop(self, system_id: UUID) -> None:
        """Finalize a collector's artifact, then forget it."""
        collector = self._collectors.pop(system_id, None)
        if collector is not None:
            self._cancel_pump(system_id)
            collector.finalize()

    async def finalize_and_drop_async(self, system_id: UUID) -> None:
        """Event-loop-safe :meth:`finalize_and_drop`."""
        collector = self._collectors.pop(system_id, None)
        if collector is None:
            return
        self._cancel_pump(system_id)
        await asyncio.to_thread(collector.finalize)

    def drop_all(self) -> None:
        """Close and forget every collector without finalizing."""
        for system_id in list(self._collectors):
            self.drop(system_id)

    def _cancel_pump(self, system_id: UUID) -> None:
        if self._pump_runner is not None:
            self._pump_runner.cancel(system_id)


class ConsoleHostingLoop:
    """Acquires leadership, opens collectors for new Systems, and pumps live ones."""

    def __init__(
        self,
        *,
        leader_lock: LeaderLock,
        running_systems: RunningSystems,
        collector_factory: CollectorFactory,
        registry: CollectorRegistry,
        pump_runner: PumpRunner | None = None,
    ) -> None:
        self._leader_lock = leader_lock
        self._running_systems = running_systems
        self._collector_factory = collector_factory
        self._registry = registry
        self._pump_runner = pump_runner
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def tick(self) -> None:
        """Reconcile leadership, then host or stop hosting."""
        try:
            if not await self._reconcile_leadership():
                return
            await self._host_running_systems()
        except Exception:  # noqa: BLE001 - durable hosting loop survives transient errors
            _log.warning("console hosting tick failed; retrying next tick", exc_info=True)

    async def _reconcile_leadership(self) -> bool:
        if self._is_leader:
            if await self._lock_still_held():
                return True
            _log.warning(
                "console hosting lost (or could not confirm) its leader lock; closing all streams"
            )
            self._stop_all_pumps()
            self._registry.drop_all()
            self._is_leader = False
            return False
        return await self._try_become_leader()

    async def _lock_still_held(self) -> bool:
        try:
            return await self._leader_lock.is_held()
        except Exception:  # noqa: BLE001 - fail closed on lock-check errors
            _log.warning(
                "console hosting leader-lock check failed; treating as a loss", exc_info=True
            )
            return False

    async def _try_become_leader(self) -> bool:
        try:
            acquired = await self._leader_lock.try_acquire()
        except Exception:  # noqa: BLE001 - failed acquire only skips this tick
            _log.warning(
                "console hosting leader-lock acquire failed; not hosting this tick", exc_info=True
            )
            return False
        if acquired:
            _log.info("console hosting acquired leadership")
        self._is_leader = acquired
        return acquired

    async def _host_running_systems(self) -> None:
        running = await self._running_systems.list_running()
        for system_id in running:
            if not self._registry.has(system_id):
                self._open_collector(system_id)
        if self._pump_runner is None:
            for system_id in self._registry.system_ids():
                self._pump(system_id)

    def _open_collector(self, system_id: UUID) -> None:
        try:
            collector = self._collector_factory(system_id)
        except Exception:  # noqa: BLE001 - one System's open failure must not stop hosting
            _log.warning("opening console collector for %s failed; will retry", system_id)
            return
        self._registry.add(collector)
        if self._pump_runner is not None:
            self._pump_runner.start(collector)
        _log.info("console attach-watcher opened a collector for %s", system_id)

    def _pump(self, system_id: UUID) -> None:
        collector = self._registry.get(system_id)
        if collector is None:
            return
        try:
            collector.pump_once()
        except Exception:  # noqa: BLE001 - one collector failure is isolated
            _log.warning("console collector pump for %s failed; isolated", system_id, exc_info=True)

    def _stop_all_pumps(self) -> None:
        if self._pump_runner is not None:
            self._pump_runner.cancel_all()

    async def stop(self) -> None:
        """Release leadership and close all streams."""
        self._stop_all_pumps()
        self._registry.drop_all()
        if self._is_leader:
            await self._leader_lock.release()
            self._is_leader = False


class AsyncioPumpRunner:
    """Production :class:`PumpRunner`: one continuous off-loop task per collector."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    def start(self, collector: Collector) -> None:
        system_id = collector.system_id
        if system_id in self._tasks:
            return
        self._tasks[system_id] = asyncio.create_task(self._pump_forever(collector))

    async def _pump_forever(self, collector: Collector) -> None:
        while True:
            try:
                got = await asyncio.to_thread(collector.pump_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - isolate one collector
                _log.warning(
                    "console pump task for %s errored; continuing",
                    collector.system_id,
                    exc_info=True,
                )
                await asyncio.sleep(1.0)
                continue
            if not got:
                await asyncio.sleep(_IDLE_PUMP_BACKOFF_SECONDS)

    def cancel(self, system_id: UUID) -> None:
        task = self._tasks.pop(system_id, None)
        if task is not None:
            task.cancel()

    def cancel_all(self) -> None:
        for system_id in list(self._tasks):
            self.cancel(system_id)


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
    """Start the hosting loop concurrently with the reconciler."""
    if console_hosting is None:
        return None
    return asyncio.create_task(console_hosting.run(stop))
