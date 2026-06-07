"""Adversarial: provision and teardown racing on one System must not leak a domain.

`provision_handler` runs the slow `provision()` **without** the SYSTEM advisory lock,
then re-reads the state under the lock and *compensates* — reaping the domain it created
— if a concurrent teardown drove the System terminal first (systems.py, ADR-0025 §8).
`teardown_handler` commits `torn_down` under the lock, then destroys unlocked.

The invariant under attack: for every interleaving of a concurrent provision + teardown
of the same System, the System ends `torn_down` and **no provisioned domain is left
live** (every domain `provision()` created was `teardown()`-reaped). The existing suite
simulates the race by flipping DB state inside the fake provider on one connection; this
test runs the two handlers as genuinely concurrent tasks on separate pooled connections.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.models import Job, JobKind, System
from kdive.domain.state import AllocationState, SystemState
from kdive.jobs import queue
from kdive.mcp.tools import control as control_tools
from kdive.mcp.tools import systems_handlers
from kdive.providers.local_libvirt.control import PowerAction
from tests.adversarial.conftest import seed_allocation, seed_resource

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {
                "kind": "path",
                "path": "oci://registry.internal/rootfs/fedora-40@sha256:abc",
            },
            "crashkernel": "256M",
        }
    },
}


class _TrackingProvisioner:
    """A fake provider that models the host's live-domain set.

    ``provision`` adds the domain; ``teardown`` removes it (idempotent discard);
    ``reprovision`` re-applies in place. A non-empty ``live`` after the race means a
    domain leaked.
    """

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.provisioned: list[UUID] = []
        self.reprovisioned: list[UUID] = []
        self.torn_down: list[str] = []

    def provision(self, system_id: UUID, profile: Any) -> str:
        name = f"kdive-{system_id}"
        self.provisioned.append(system_id)
        self.live.add(name)
        return name

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)
        self.live.discard(domain_name)

    def reprovision(self, system_id: UUID, profile: Any) -> str:
        name = f"kdive-{system_id}"
        self.reprovisioned.append(system_id)
        self.live.add(name)
        return name


class _RecordingController:
    """Records control ops; force_crash/power never raise (the live host would)."""

    def __init__(self) -> None:
        self.crashed: list[str] = []
        self.powered: list[tuple[str, PowerAction]] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.powered.append((domain_name, action))

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(
    pool: AsyncConnectionPool, state: SystemState, *, domain_name: str | None = None
) -> str:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=state,
                provisioning_profile=_PROFILE,
                domain_name=domain_name,
            ),
        )
    return str(system.id)


async def _enqueue(pool: AsyncConnectionPool, kind: JobKind, system_id: str, dedup: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            kind,
            {"system_id": system_id},
            {"principal": "alice", "agent_session": "s", "project": "proj"},
            dedup,
        )


async def _system_state(pool: AsyncConnectionPool, system_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return row["state"]


async def _race_once(pool: AsyncConnectionPool, *, provision_first: bool) -> tuple[str, set[str]]:
    system_id = await _seed_system(pool, SystemState.PROVISIONING)
    prov = _TrackingProvisioner()
    pjob = await _enqueue(pool, JobKind.PROVISION, system_id, f"{system_id}:provision")
    tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

    async def run_provision() -> None:
        async with pool.connection() as conn:
            await systems_handlers.provision_handler(conn, pjob, prov)

    async def run_teardown() -> None:
        async with pool.connection() as conn:
            await systems_handlers.teardown_handler(conn, tjob, prov)

    tasks = (
        [run_provision(), run_teardown()] if provision_first else [run_teardown(), run_provision()]
    )
    await asyncio.gather(*tasks)
    return await _system_state(pool, system_id), prov.live


def test_concurrent_provision_teardown_never_leaks_a_domain(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):  # sample interleavings, both start orders
                state, live = await _race_once(pool, provision_first=(i % 2 == 0))
                assert state == "torn_down", (
                    f"iteration {i}: System ended {state!r}, want torn_down"
                )
                assert live == set(), f"iteration {i}: leaked domain(s): {live}"

    asyncio.run(_run())


def test_concurrent_force_crash_and_teardown_end_torn_down_no_stale_nmi(migrated_url: str) -> None:
    # force_crash (ready->crashed) and teardown (->torn_down) both hold LockScope.SYSTEM for
    # their whole transition. Whatever the order: the System ends torn_down (teardown is the
    # terminal sink), the domain is reaped, and the NMI fires at most once and never against a
    # System the lock already shows terminal (force_crash's terminal-state early return).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):
                system_id = await _seed_system(pool, SystemState.READY)
                prov = _TrackingProvisioner()
                prov.live.add(f"kdive-{system_id}")  # the live System's domain
                ctrl = _RecordingController()
                cjob = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:crash")
                tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

                async def run_crash(job: Job = cjob, ctrl: _RecordingController = ctrl) -> None:
                    async with pool.connection() as conn:
                        await control_tools.force_crash_handler(conn, job, ctrl)

                async def run_teardown(job: Job = tjob, prov: _TrackingProvisioner = prov) -> None:
                    async with pool.connection() as conn:
                        await systems_handlers.teardown_handler(conn, job, prov)

                order = [run_crash(), run_teardown()] if i % 2 else [run_teardown(), run_crash()]
                await asyncio.gather(*order)

                assert await _system_state(pool, system_id) == "torn_down"
                assert prov.live == set(), f"iteration {i}: leaked domain {prov.live}"
                assert len(ctrl.crashed) <= 1

    asyncio.run(_run())


def test_concurrent_double_teardown_is_idempotent(migrated_url: str) -> None:
    # A single teardown job double-dispatched (lease lapse -> two handler runs) must converge:
    # both runs succeed, the System ends torn_down exactly once, and the domain is reaped.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(10):
                system_id = await _seed_system(pool, SystemState.READY)
                prov = _TrackingProvisioner()
                prov.live.add(f"kdive-{system_id}")
                job = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

                async def run(j: Job = job, prov: _TrackingProvisioner = prov) -> str | None:
                    async with pool.connection() as conn:
                        return await systems_handlers.teardown_handler(conn, j, prov)

                results = await asyncio.gather(run(), run())
                assert all(r == system_id for r in results)
                assert await _system_state(pool, system_id) == "torn_down"
                assert prov.live == set(), f"iteration {i}: leaked domain {prov.live}"
                # Exactly one ready->torn_down audit transition despite two runs.
                async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT count(*) AS n FROM audit_log "
                        "WHERE object_id = %s AND transition = 'ready->torn_down'",
                        (UUID(system_id),),
                    )
                    row = await cur.fetchone()
                assert row is not None and row["n"] == 1

    asyncio.run(_run())
