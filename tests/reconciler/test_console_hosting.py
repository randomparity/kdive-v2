"""Tests for the single-leader console-hosting loop + attach-watcher (ADR-0095)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from kdive.providers.console_hosting import (
    CollectorRegistry,
    ConsoleHostingLoop,
)


class FakeCollector:
    """A stand-in collector recording pump/finalize/close, satisfying the registry's contract."""

    def __init__(self, system_id) -> None:  # noqa: ANN001
        self.system_id = system_id
        self.pumps = 0
        self.finalized = False
        self.closed = False
        self.raise_on_pump = False

    def pump_once(self) -> bool:
        self.pumps += 1
        if self.raise_on_pump:
            raise RuntimeError("pump boom")
        return True

    def finalize(self) -> None:
        self.finalized = True

    def close(self) -> None:
        self.closed = True


class FakeLeaderLock:
    def __init__(self, *, acquirable: bool = True) -> None:
        self.acquirable = acquirable
        self.held = False
        self.released = False

    async def try_acquire(self) -> bool:
        if self.acquirable:
            self.held = True
        return self.held

    async def is_held(self) -> bool:
        return self.held

    async def release(self) -> None:
        self.held = False
        self.released = True


class FakeRunning:
    def __init__(self) -> None:
        self.systems: set = set()

    async def list_running(self) -> set:
        return set(self.systems)


def _loop(lock, running, *, registry=None):  # noqa: ANN001
    reg = registry or CollectorRegistry()
    made: dict = {}

    def default_factory(system_id):  # noqa: ANN001
        collector = FakeCollector(system_id)
        made[system_id] = collector
        return collector

    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=running,
        collector_factory=default_factory,
        registry=reg,
    )
    return loop, reg, made


def test_non_leader_hosts_nothing() -> None:
    lock = FakeLeaderLock(acquirable=False)
    running = FakeRunning()
    running.systems = {uuid4()}
    loop, reg, made = _loop(lock, running)
    asyncio.run(loop.tick())
    assert loop.is_leader is False
    assert reg.system_ids() == set()
    assert made == {}


def test_leader_attach_watcher_opens_collector_for_new_system() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    loop, reg, made = _loop(lock, running)
    asyncio.run(loop.tick())
    assert loop.is_leader is True
    assert reg.has(sid)
    assert made[sid].pumps == 1


def test_attach_watcher_does_not_reopen_existing_collector() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    loop, reg, made = _loop(lock, running)

    async def _run() -> None:
        await loop.tick()
        await loop.tick()

    asyncio.run(_run())
    assert len(made) == 1
    assert made[sid].pumps == 2


def test_lock_loss_closes_all_streams_before_reacquire() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    loop, reg, made = _loop(lock, running)

    async def _run() -> None:
        await loop.tick()  # becomes leader, opens collector
        assert reg.has(sid)
        # Simulate a dropped leader connection: Postgres released the lock with no notice.
        lock.held = False
        await loop.tick()

    asyncio.run(_run())
    # The loop must have closed the open stream and stopped hosting (no finalize on failover).
    assert made[sid].closed is True
    assert made[sid].finalized is False
    assert reg.system_ids() == set()
    assert loop.is_leader is False


def test_lock_loss_does_not_reacquire_in_same_tick() -> None:
    # AC6: on loss the loop closes streams and returns; it must NOT re-acquire and re-open in
    # the same tick (that would race a standby that already took the lock).
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    loop, reg, made = _loop(lock, running)
    first_holder: dict = {}

    async def _run() -> None:
        await loop.tick()
        first_holder["c"] = made[sid]
        lock.held = False
        await loop.tick()
        assert reg.system_ids() == set()
        # A later tick may re-acquire and cold-start a fresh collector.
        await loop.tick()

    asyncio.run(_run())
    assert reg.has(sid)
    assert made[sid] is not first_holder["c"]


def test_pump_failure_is_isolated() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    good, bad = uuid4(), uuid4()
    running.systems = {good, bad}
    loop, reg, made = _loop(lock, running)

    async def _run() -> None:
        await loop.tick()  # opens both
        made[bad].raise_on_pump = True
        await loop.tick()  # bad raises, good still pumps

    asyncio.run(_run())
    assert made[good].pumps == 2
    assert reg.has(good)
    assert reg.has(bad)  # a transient pump failure does not drop the collector


def test_stop_releases_lock_and_closes_streams() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    loop, reg, made = _loop(lock, running)

    async def _run() -> None:
        await loop.tick()
        await loop.stop()

    asyncio.run(_run())
    assert made[sid].closed is True
    assert made[sid].finalized is False
    assert lock.released is True
    assert reg.system_ids() == set()


class FakePumpRunner:
    def __init__(self) -> None:
        self.started: list = []
        self.cancelled: list = []
        self.cancel_all_calls = 0

    def start(self, collector) -> None:  # noqa: ANN001
        self.started.append(collector.system_id)

    def cancel(self, system_id) -> None:  # noqa: ANN001
        self.cancelled.append(system_id)

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1


def test_pump_runner_starts_per_collector_and_does_not_inline_pump() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    runner = FakePumpRunner()
    reg = CollectorRegistry(pump_runner=runner)
    made: dict = {}

    def factory(system_id):  # noqa: ANN001
        c = FakeCollector(system_id)
        made[system_id] = c
        return c

    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=running,
        collector_factory=factory,
        registry=reg,
        pump_runner=runner,
    )
    asyncio.run(loop.tick())
    assert runner.started == [sid]
    assert made[sid].pumps == 0  # production pumps off-loop, not inline


def test_pump_runner_cancel_all_on_lock_loss() -> None:
    lock = FakeLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    runner = FakePumpRunner()
    reg = CollectorRegistry(pump_runner=runner)
    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=running,
        collector_factory=lambda s: FakeCollector(s),
        registry=reg,
        pump_runner=runner,
    )

    async def _run() -> None:
        await loop.tick()
        lock.held = False
        await loop.tick()

    asyncio.run(_run())
    assert runner.cancel_all_calls >= 1
    assert reg.system_ids() == set()


def test_registry_cancels_pump_on_finalize_and_drop() -> None:
    runner = FakePumpRunner()
    reg = CollectorRegistry(pump_runner=runner)
    sid = uuid4()
    reg.add(FakeCollector(sid))
    reg.finalize_and_drop(sid)
    assert runner.cancelled == [sid]


class RaisingLeaderLock(FakeLeaderLock):
    def __init__(self) -> None:
        super().__init__()
        self.raise_on_is_held = False

    async def is_held(self) -> bool:
        if self.raise_on_is_held:
            raise RuntimeError("leader connection dropped")
        return self.held


def test_lock_check_error_is_treated_as_loss_and_does_not_kill_tick() -> None:
    # A leader-lock check that raises (dead connection) must be fail-closed: stop hosting and
    # retry, not propagate and strand the hosting task.
    lock = RaisingLeaderLock()
    running = FakeRunning()
    sid = uuid4()
    running.systems = {sid}
    runner = FakePumpRunner()
    reg = CollectorRegistry(pump_runner=runner)
    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=running,
        collector_factory=lambda s: FakeCollector(s),
        registry=reg,
        pump_runner=runner,
    )

    async def _run() -> None:
        await loop.tick()  # becomes leader, opens collector
        assert reg.has(sid)
        lock.raise_on_is_held = True
        await loop.tick()  # is_held raises -> treated as a loss, no exception escapes

    asyncio.run(_run())
    assert reg.system_ids() == set()
    assert loop.is_leader is False


def test_tick_survives_a_running_systems_query_error() -> None:
    class BoomRunning:
        async def list_running(self):  # noqa: ANN202
            raise RuntimeError("db down")

    lock = FakeLeaderLock()
    loop = ConsoleHostingLoop(
        leader_lock=lock,
        running_systems=BoomRunning(),
        collector_factory=lambda s: FakeCollector(s),
        registry=CollectorRegistry(),
    )
    # Must not raise: a transient query failure is logged and retried next tick.
    asyncio.run(loop.tick())


def test_registry_finalize_and_drop_persists_then_forgets() -> None:
    reg = CollectorRegistry()
    sid = uuid4()
    collector = FakeCollector(sid)
    reg.add(collector)
    reg.finalize_and_drop(sid)
    assert collector.finalized is True
    assert reg.has(sid) is False


def test_registry_drop_closes_without_finalize() -> None:
    reg = CollectorRegistry()
    sid = uuid4()
    collector = FakeCollector(sid)
    reg.add(collector)
    reg.drop(sid)
    assert collector.closed is True
    assert collector.finalized is False
    assert reg.has(sid) is False
