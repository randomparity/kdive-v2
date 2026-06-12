"""Each reconciler drift-repair pass driven by a real, seed-pinned fault-inject fault.

ADR-0074 / spec m1.5 §Validation surface (issue 5). The reconciler passes already exist and
are unit-tested with hand-seeded rows (``tests/reconciler/test_loop.py``); these tests prove
the **fault engine** and the **mock infra-inventory** produce the drift each pass repairs,
asserting the exact post-repair state. Five cases, each pinned:

- **orphaned System** — a *successful* (no-fail) fault-inject provision mints a live System;
  its allocation is then released → ``_repair_orphaned_systems`` enqueues teardown.
- **abandoned job** — a run-bearing zombie (lapsed lease + exhausted attempts) is dead-lettered
  and its Run compensated (worker-death framing of the same sweep).
- **dead DebugSession** — a ``connect`` ``TRANSPORT_FAILURE`` draw is the upstream cause of a
  stale heartbeat; ``_repair_dead_sessions`` detaches the ``live`` session on the staleness.
- **leaked provider infra** — the mock ``FaultInjectReaper`` reports a domain with no owning
  System row → ``_repair_leaked_domains`` reaps it via the real seam (not ``FakeReaper``).
- **lease-expiry-mid-job** — the engine's ``latency_s`` (asserted ``>`` the lease) is what the
  ``FaultedInstall`` would block on; the job's lapsed lease drives the owning Run to
  ``failed(lease_expired)``, distinct from ``canceled`` and from any catalog category.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    RunState,
    SystemState,
)
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.faulting.engine import FaultEngine, FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory, FaultInjectReaper
from kdive.providers.fault_inject.lifecycle.faulted import FaultedInstall, FaultedProvision
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvision
from kdive.providers.ports import InstallRequest
from kdive.reconciler import loop
from tests.reconciler.conftest import (
    connect,
    run_repair,
    seed_debug_session,
    seed_run,
    seed_running_job,
    seed_system,
)

# A seed certain to fail / never fail the named plane (fault_rate boundary).
_FAIL_SEED = 7
_PROFILE = cast(ProvisioningProfile, object())


def _no_fail_engine(plane: FaultPlane, *, max_latency_s: float = 0.0) -> FaultEngine:
    return FaultEngine(
        seed=_FAIL_SEED, fault_rate={plane.value: 0.0}, max_latency_s={plane.value: max_latency_s}
    )


def _fail_engine(plane: FaultPlane) -> FaultEngine:
    return FaultEngine(seed=_FAIL_SEED, fault_rate={plane.value: 1.0}, max_latency_s={})


def _noop_sleep(_delay: float) -> None:
    return None


# --- leaked provider infra (mock inventory seam, no engine draw) -----------------------


def test_leaked_fault_inject_domain_is_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        # A synthetic domain owned by a System with NO row at all -> a leak.
        inventory = FaultInjectInventory()
        orphan_system = uuid4()
        domain = f"fault-inject-{orphan_system}"
        FaultInjectProvision(inventory).provision(orphan_system, _PROFILE)
        assert any(d.name == domain for d in inventory.owned_domains())
        reaper = FaultInjectReaper(inventory)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 1
        # The real mock seam forgot the domain (idempotent destroy) -> no longer owned.
        assert all(d.name != domain for d in inventory.owned_domains())

    asyncio.run(_run())


def test_fault_inject_domain_with_live_row_not_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed, system_state=SystemState.READY)
        inventory = FaultInjectInventory()
        domain = f"fault-inject-{system_id}"
        FaultInjectProvision(inventory).provision(system_id, _PROFILE)
        reaper = FaultInjectReaper(inventory)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: loop._repair_leaked_domains(conn, reaper))
        assert count == 0
        assert any(d.name == domain for d in inventory.owned_domains())  # live row protects it

    asyncio.run(_run())


# --- orphaned System (successful provision, then allocation released) ------------------


def test_orphaned_system_after_successful_fault_inject_provision(migrated_url: str) -> None:
    async def _run() -> None:
        # (a) a live System on an ACTIVE allocation; (b) a real no-fail wrapper provision mints
        # and records its domain; (c) the allocation is released; (d) reconcile -> teardown.
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.READY, alloc_state=AllocationState.ACTIVE
            )
        inventory = FaultInjectInventory()
        wrapper = FaultedProvision(
            FaultInjectProvision(inventory),
            _no_fail_engine(FaultPlane.PROVISION),
            sleep_s=_noop_sleep,
        )
        domain = wrapper.provision(system_id, _PROFILE)  # no-fail draw -> delegates, records
        assert domain == f"fault-inject-{system_id}"
        async with await connect(migrated_url) as seed:
            await seed.execute(
                "UPDATE allocations SET state = %s WHERE id = "
                "(SELECT allocation_id FROM systems WHERE id = %s)",
                (AllocationState.RELEASED.value, system_id),
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT kind, authorizing FROM jobs WHERE dedup_key = %s",
                (f"{system_id}:teardown",),
            )
            job = await cur.fetchone()
            assert job is not None
            assert job[0] == "teardown"
            assert job[1]["principal"] == "system:reconciler"

    asyncio.run(_run())


def test_fail_drawn_system_is_failed_not_orphan_reaped(migrated_url: str) -> None:
    async def _run() -> None:
        # A provision FAIL draw drives System -> failed, which the orphan reaper EXCLUDES
        # (_ORPHANED_SYSTEM_TERMINAL_STATES includes FAILED). Locks ADR-0074's decoupling:
        # a fail draw is the wrong trigger for the orphaned-System case.
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(
                seed, system_state=SystemState.FAILED, alloc_state=AllocationState.RELEASED
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_orphaned_systems)
        assert count == 0  # a failed System is not orphan-reaped
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT count(*) FROM jobs WHERE dedup_key = %s", (f"{system_id}:teardown",)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == 0

    asyncio.run(_run())


# --- abandoned job (worker death) ------------------------------------------------------


def test_abandoned_run_bearing_job_dead_lettered_and_run_compensated(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id, run_state=RunState.RUNNING)
            job_id = await seed_running_job(
                seed,
                "dk-abandoned-install",
                kind="install",
                payload={"run_id": str(run_id)},
                lease_seconds=-60,  # worker died: lease lapsed
                attempt=3,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, error_category FROM jobs WHERE id = %s", (job_id,)
            )
            job = await cur.fetchone()
            assert job is not None and job[0] == "failed" and job[1] == "lease_expired"
            cur = await check.execute(
                "SELECT state, failure_category FROM runs WHERE id = %s", (run_id,)
            )
            run = await cur.fetchone()
            assert run is not None and run[0] == "failed" and run[1] == "lease_expired"

    asyncio.run(_run())


# --- dead DebugSession (connect transport-drop -> stale heartbeat -> detached) ---------


def test_dead_session_from_connect_transport_drop_is_detached(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
        # The upstream cause, on THIS system: a fail-certain seed drops its connect transport;
        # a no-fail seed for the same system does not — so the seed (not just the single-entry
        # catalog) is doing the work.
        dropped = _fail_engine(FaultPlane.CONNECT).decide(
            system_id=system_id, plane=FaultPlane.CONNECT, attempt=1
        )
        assert dropped.fail is True
        assert dropped.category is ErrorCategory.TRANSPORT_FAILURE  # the dropped transport
        intact = _no_fail_engine(FaultPlane.CONNECT).decide(
            system_id=system_id, plane=FaultPlane.CONNECT, attempt=1
        )
        assert intact.fail is False  # seed-sensitive: no drop without a failing rate
        # The reconciler trigger is the stale heartbeat the dropped transport produces (the
        # worker stopped beating), NOT the fault category.
        async with await connect(migrated_url) as seed:
            session_id = await seed_debug_session(
                seed, run_id, state=DebugSessionState.LIVE, heartbeat_ago=timedelta(hours=1)
            )
        from kdive.providers.transport_reset import NullResetter

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(
                pool,
                lambda conn: loop._repair_dead_sessions(
                    conn, loop.DEFAULT_DEBUG_SESSION_STALE_AFTER, NullResetter()
                ),
            )
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state FROM debug_sessions WHERE id = %s", (session_id,)
            )
            row = await cur.fetchone()
            assert row is not None and row[0] == "detached"

    asyncio.run(_run())


# --- lease-expiry-mid-job (latency lever; Run failed(lease_expired) != canceled) -------


def test_lease_expiry_mid_install_fails_run_lease_expired_not_canceled(migrated_url: str) -> None:
    async def _run() -> None:
        # The engine's install latency is the lever. Compute it, prove the FaultedInstall would
        # block for exactly it, and choose a lease window provably shorter than it. A fixed
        # system_id pins the seed-derived latency so the lease/latency relation is deterministic.
        system_id = UUID("00000000-0000-0000-0000-00000000fa11")
        engine = _no_fail_engine(FaultPlane.INSTALL, max_latency_s=1000.0)
        latency_s = engine.decide(
            system_id=system_id, plane=FaultPlane.INSTALL, attempt=1
        ).latency_s
        assert latency_s >= 2.0  # a real multi-second delay (pinned seed) -> a positive lease

        recorded: list[float] = []
        FaultedInstall(FaultInjectInstall(), engine, sleep_s=recorded.append).install(
            InstallRequest(
                system_id=system_id,
                run_id=uuid4(),
                kernel_ref="kernel-ref",
                cmdline="console=ttyS0",
            )
        )
        assert recorded == [latency_s]  # the wrapper would block install for exactly this delay

        lease_window = int(latency_s) // 2
        assert 1 <= lease_window < latency_s  # the engine delay provably outlasts the lease

        async with await connect(migrated_url) as seed:
            seeded_system = await seed_system(seed)
            run_id = await seed_run(seed, seeded_system, run_state=RunState.RUNNING)
            job_id = await seed_running_job(
                seed,
                "dk-lease-expiry-install",
                kind="install",
                payload={"run_id": str(run_id)},
                lease_seconds=-lease_window,  # the lapsed state the slow install produces
                attempt=3,
                max_attempts=3,
            )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, loop._repair_abandoned_jobs)
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute(
                "SELECT state, error_category FROM jobs WHERE id = %s", (job_id,)
            )
            job = await cur.fetchone()
            assert job is not None and job[0] == "failed" and job[1] == "lease_expired"
            cur = await check.execute(
                "SELECT state, failure_category FROM runs WHERE id = %s", (run_id,)
            )
            run = await cur.fetchone()
            assert run is not None
            assert run[0] == RunState.FAILED.value
            assert run[0] != RunState.CANCELED.value  # distinct from canceled
            assert run[1] == ErrorCategory.LEASE_EXPIRED.value  # not a catalog category

    asyncio.run(_run())
