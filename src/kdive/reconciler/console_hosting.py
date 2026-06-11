"""Single-leader console-collector hosting loop + attach-watcher (ADR-0095).

The reconciler is the only long-lived control component, so it hosts the per-System console
streamers. Hosting must run on **exactly one** process: the hosted `virDomainOpenConsole`
streams are not transaction-lock-guarded, so two replicas would open duplicate streams per
System. This loop gates all hosting behind a **session-scoped** leadership lock
(`SessionAdvisoryLock`, ADR-0095) held on a dedicated connection outside the repair pool.

Two hazards drive the structure:

- **Prompt attach (AC4).** A `virDomainOpenConsole` stream is future-only and bound to the
  opening process, so it cannot be handed worker→leader. The leader runs a continuous
  attach-watcher at sub-tick cadence that opens a stream for any running remote System lacking
  a live collector — decoupled from the 30s repair pass so early-boot console is not gapped.
- **Split-brain on lock loss (AC6).** A session lock is released by Postgres the instant the
  holding connection drops, so a standby can acquire it while the old leader is unaware. On
  **any** lock/connection loss this loop **immediately stops hosting and closes all open
  streams before any re-acquire**, so a failover window has at most one host per System.

The reap/liveness `reconcile_once` class (`reconciler/loop.py`) operates on the **same**
:class:`CollectorRegistry`: it restarts a dead stream and reaps a gone System's collector only
after any teardown-finalize has persisted the artifact (AC7). Every seam — the leader lock,
the running-System source, the collector factory — is injected so the loop is unit-testable
without Postgres or a libvirt host.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import ResourceKind
from kdive.domain.state import SystemState

_log = logging.getLogger(__name__)

# A remote System in one of these states has a live domain whose console should be streamed.
# Terminal states (torn_down, failed) and pre-domain states (defined, provisioning) are excluded.
_RUNNING_SYSTEM_STATE_VALUES = (
    SystemState.READY.value,
    SystemState.REPROVISIONING.value,
    SystemState.CRASHED.value,
)
_REMOTE_KIND_VALUE = ResourceKind.REMOTE_LIBVIRT.value

# Back-off between pump reads that return no data (idle console, EOF, or a dropped stream
# pending reconnect) so an inactive console never busy-loops the off-loop pump task.
_IDLE_PUMP_BACKOFF_SECONDS = 0.5


class Collector(Protocol):
    """The structural collector contract the registry/hosting loop drive (ConsoleCollector).

    A Protocol, not the concrete class, so the reap class and the hosting loop are testable with
    a fake collector and the production :class:`ConsoleCollector` satisfies it by construction.
    """

    @property
    def system_id(self) -> UUID: ...
    def pump_once(self) -> bool: ...
    def finalize(self) -> None: ...
    def close(self) -> None: ...


class LeaderLock(Protocol):
    """The session-scoped leadership claim the hosting loop gates on (SessionAdvisoryLock)."""

    async def try_acquire(self) -> bool: ...
    async def is_held(self) -> bool: ...
    async def release(self) -> None: ...


class RunningSystems(Protocol):
    """Reports the running remote Systems that should have a live console collector."""

    async def list_running(self) -> set[UUID]: ...


class CollectorFactory(Protocol):
    """Builds a per-System collector (binds the System's OpenConsole stream + part store)."""

    def __call__(self, system_id: UUID, /) -> Collector: ...


class PumpRunner(Protocol):
    """Drives a collector's continuous pumping off the event loop (production wiring).

    A blocking ``virDomainOpenConsole`` recv must not stall the hosting loop, so production
    runs each collector's pump in its own task that offloads the blocking read (e.g. via
    ``asyncio.to_thread``). The unit-tested ``tick`` cadence drives pumping inline when no
    runner is injected, keeping the leadership/attach/lock-loss logic testable without tasks.
    """

    def start(self, collector: Collector) -> None: ...
    def cancel(self, system_id: UUID) -> None: ...
    def cancel_all(self) -> None: ...


class CollectorRegistry:
    """The live per-System collectors, shared by the hosting loop and the reap class.

    Hosting adds and pumps collectors; the reap class restarts dead ones and finalizes+drops
    collectors for gone Systems. ``drop`` closes a collector's stream **without** finalizing
    (the lock-loss / failover path — the new leader cold-starts); ``finalize_and_drop`` persists
    the artifact first (the teardown / reap path, AC7).
    """

    def __init__(self, pump_runner: PumpRunner | None = None) -> None:
        self._collectors: dict[UUID, Collector] = {}
        # When set (production), the registry cancels a collector's pump task as it drops it, so
        # the reap class (which only holds the registry) tears down the task with the collector.
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
        """Close and forget a collector **without** finalizing (failover / split-brain path).

        The hosted stream is closed so it stops streaming before any standby re-opens it; the
        artifact is intentionally not assembled — on failover the new leader cold-starts and
        pre-failover history is the accepted best-effort loss (ADR-0095).
        """
        collector = self._collectors.pop(system_id, None)
        if collector is not None:
            self._cancel_pump(system_id)
            collector.close()

    def finalize_and_drop(self, system_id: UUID) -> None:
        """Finalize a collector's artifact, then forget it (teardown / reap path, AC7).

        The pump task is cancelled **before** finalize so no concurrent pump appends a new part
        between assembly and drop; finalize then reads a settled part set. ``finalize`` does
        blocking store I/O — callers on the event loop must use :meth:`finalize_and_drop_async`
        instead so the reconciler loop is not stalled.
        """
        collector = self._collectors.pop(system_id, None)
        if collector is not None:
            self._cancel_pump(system_id)
            collector.finalize()

    async def finalize_and_drop_async(self, system_id: UUID) -> None:
        """Event-loop-safe :meth:`finalize_and_drop`: offload the blocking finalize to a thread.

        Cancels the pump task on the loop first (an asyncio cancel must run on the loop), then
        runs the collector's blocking finalize (object-store puts + a Postgres row upsert) in a
        worker thread so a reap pass never blocks the reconciler event loop on network I/O.
        """
        collector = self._collectors.pop(system_id, None)
        if collector is None:
            return
        self._cancel_pump(system_id)
        await asyncio.to_thread(collector.finalize)

    def _cancel_pump(self, system_id: UUID) -> None:
        if self._pump_runner is not None:
            self._pump_runner.cancel(system_id)

    def drop_all(self) -> None:
        """Close and forget every collector (lock-loss guard, AC6) — no finalize."""
        for system_id in list(self._collectors):
            self.drop(system_id)


class ConsoleHostingLoop:
    """Acquires leadership, opens collectors for new Systems, and pumps live ones.

    Driven step-wise by :meth:`tick` (the attach-watcher cadence) so the logic is unit-testable
    without a real event loop; the process entrypoint calls :meth:`tick` in a sub-tick loop
    sharing the reconciler's stop event.
    """

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
        """One hosting step: reconcile leadership, then host (or stop hosting) accordingly.

        The leadership check runs **first and every tick**: if the loop believed it was leader
        but the lock is no longer held (a dropped connection released it), it drops all
        collectors before doing anything else (AC6). A non-leader hosts nothing (AC5). A tick
        never raises: a transient error (a failed running-Systems query, a factory error) is
        logged and retried next tick so the hosting task is durable.
        """
        try:
            if not await self._reconcile_leadership():
                return
            await self._host_running_systems()
        except Exception:  # noqa: BLE001 - a durable hosting loop survives a transient tick error
            _log.warning("console hosting tick failed; retrying next tick", exc_info=True)

    async def _reconcile_leadership(self) -> bool:
        """Return whether this loop holds leadership after reconciling, closing streams on loss.

        Order matters for the split-brain guard: a leader that lost its lock closes all streams
        **before** any re-acquire attempt, so a standby that already grabbed the lock is never
        joined by the old leader still streaming. A **failed** lock check (e.g. the leader
        connection raised) is treated as a loss — fail-closed: stop hosting and retry next tick,
        rather than letting an exception kill the hosting task and strand the streams.
        """
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
        except Exception:  # noqa: BLE001 - a lock-check error is fail-closed: treat as a loss
            _log.warning(
                "console hosting leader-lock check failed; treating as a loss", exc_info=True
            )
            return False

    async def _try_become_leader(self) -> bool:
        try:
            acquired = await self._leader_lock.try_acquire()
        except Exception:  # noqa: BLE001 - a failed acquire just means not-leader this tick
            _log.warning(
                "console hosting leader-lock acquire failed; not hosting this tick", exc_info=True
            )
            return False
        if acquired:
            _log.info("console hosting acquired leadership")
        self._is_leader = acquired
        return acquired

    async def _host_running_systems(self) -> None:
        """Open a collector for every running System lacking one, then pump live ones.

        With a production ``pump_runner`` each collector pumps in its own off-loop task, so the
        attach-watcher only opens; without one (tests) it pumps inline at the tick cadence.
        """
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
        except Exception:  # noqa: BLE001 - one System's open failure must not stop the watcher
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
        except Exception:  # noqa: BLE001 - one collector's failure is isolated from the rest
            _log.warning("console collector pump for %s failed; isolated", system_id, exc_info=True)

    def _stop_all_pumps(self) -> None:
        if self._pump_runner is not None:
            self._pump_runner.cancel_all()

    async def stop(self) -> None:
        """Release leadership and close all streams (process shutdown, cancel-on-stop)."""
        self._stop_all_pumps()
        self._registry.drop_all()
        if self._is_leader:
            await self._leader_lock.release()
            self._is_leader = False


class DbRunningRemoteSystems:
    """Production :class:`RunningSystems`: the running remote Systems from Postgres.

    Selects remote-libvirt Systems with a live domain (state ready/reprovisioning/crashed and a
    non-null ``domain_name``) on a fresh pooled connection per call.
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


class AsyncioPumpRunner:
    """Production :class:`PumpRunner`: one continuous off-loop pump task per collector.

    Each collector's blocking ``virDomainOpenConsole`` recv is offloaded with
    ``asyncio.to_thread`` so it never stalls the hosting loop; the task loops until the
    collector ends or the task is cancelled (drop / reap / shutdown).
    """

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
            except Exception:  # noqa: BLE001 - isolate one collector; the runner keeps the rest
                _log.warning(
                    "console pump task for %s errored; continuing",
                    collector.system_id,
                    exc_info=True,
                )
                await asyncio.sleep(1.0)
                continue
            if not got:
                # No data this read (idle/EOF/dropped stream): back off briefly so an idle or
                # powered-off console does not busy-loop the thread pool and the reconnect.
                await asyncio.sleep(_IDLE_PUMP_BACKOFF_SECONDS)

    def cancel(self, system_id: UUID) -> None:
        task = self._tasks.pop(system_id, None)
        if task is not None:
            task.cancel()

    def cancel_all(self) -> None:
        for system_id in list(self._tasks):
            self.cancel(system_id)
