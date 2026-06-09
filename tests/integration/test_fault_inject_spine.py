"""The fault-inject happy-path spine resolves the mock runtime through the real handler.

#181's acceptance is a fault-inject System provisioning end to end. The full
provision->...->capture path "in a fault-injection deployment" is the operator/live_stack
gate (it touches the object store), but the load-bearing new wiring this issue adds — the
worker resolving the *fault-inject* runtime per-System (job->system->allocation->
resource.kind) and the mock's synthetic output flowing through the real handler into the
System row — is CI-runnable against disposable Postgres (ADR-0019: drive handlers directly).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.models import Allocation, JobKind, Resource, ResourceKind, System
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import AllocationState, ResourceStatus, SystemState
from kdive.jobs import queue
from kdive.jobs.payloads import Authorizing, SystemPayload
from kdive.planes import systems as systems_handlers
from kdive.providers.composition import build_provider_resolver
from tests.integration._seed import provisioning_profile

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")


async def _seed_faultinject_system(pool: AsyncConnectionPool) -> str:
    """Insert a fault-inject Resource + granted Allocation + a `provisioning` System."""
    async with pool.connection() as conn:
        resource = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.FAULT_INJECT,
                capabilities={CONCURRENT_ALLOCATION_CAP_KEY: 2, "vcpus": 8, "memory_mb": 8192},
                pool="fault-inject",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="fault-inject://local",
            ),
        )
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=resource.id,
                state=AllocationState.ACTIVE,
            ),
        )
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
                state=SystemState.PROVISIONING,
                provisioning_profile=provisioning_profile(),
                domain_name=None,
            ),
        )
    return str(system.id)


async def _system_row(pool: AsyncConnectionPool, system_id: str) -> dict[str, object]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state, domain_name FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return row


def test_provision_routes_to_the_fault_inject_runtime_and_records_its_domain(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        pool = AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False)
        await pool.open()
        try:
            system_id = await _seed_faultinject_system(pool)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn, JobKind.PROVISION, SystemPayload(system_id=system_id), _AUTH, system_id
                )
            # No explicit provisioner: the handler must resolve the System's runtime by its
            # Resource kind (fault-inject) through the opt-in resolver.
            resolver = build_provider_resolver(enable_fault_inject=True)
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, resolver=resolver)

            row = await _system_row(pool, system_id)
            assert row["state"] == SystemState.READY.value
            # The mock's synthetic domain name (fault-inject-<system_id>) flowed through the
            # real handler into the System row — proof the fault-inject runtime was resolved.
            assert row["domain_name"] == f"fault-inject-{UUID(system_id)}"
        finally:
            await pool.close()

    asyncio.run(_run())
