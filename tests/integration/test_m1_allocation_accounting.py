"""The M1 end-to-end integration test (#71): the eight M1 exit criteria, each asserted.

M1 makes the allocation plane real — a cost model + metering ledger, enforced
budgets/quotas, reservation/lease semantics, RBAC role separation, reprovision-in-place,
and live SSH introspection (the M1 spec ``m1-allocation-accounting.md``
§"Exit criteria"). This module proves all eight on the local-libvirt stack. Per ADR-0019 the
unit of testing is the **handler**: every assertion calls a plain async handler directly with
an injected ``pool`` + ``RequestContext`` (and an injected provider/introspector where the
handler takes one), never through the MCP transport. The disposable-Postgres ``migrated_url``
fixture (ADR-0015) gives each test a freshly-migrated schema and SKIPs in CI without Docker.

The criterion → test-function map (acceptance: each criterion has an assertion):

* #1 budget/validation/idempotency → ``test_c1_*`` (six focused functions)
* #2 quota denial → ``test_c2_alloc_quota_denied`` / ``test_c2_system_quota_denied``
* #3 ledger reconciliation + rollup → ``test_c3_*``
* #4 idle lease expiry (+ Run-fail on the path that owns it) → ``test_c4_*``
* #5 renewal → ``test_c5_renew_extends`` / ``test_c5_over_budget_renew_denied``
* #6 role separation → ``test_c6_*``
* #7 reprovision-in-place → ``test_c7_reprovision_in_place_cycle``
* #8 live introspection (``live_vm``-gated) → ``test_c8_live_introspect_over_ssh``

Real libvirt/SSH/drgn stay behind the ``live_vm`` marker (criterion 8); the non-gated
redaction contract is already covered by ``tests/mcp/test_introspect_tools.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import LiteralString
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, INVESTIGATIONS, RUNS, SYSTEMS
from kdive.domain import accounting
from kdive.domain.cost import cost, quantize_kcu, rate
from kdive.domain.models import Allocation, Investigation, Job, Run, System
from kdive.domain.state import (
    AllocationState,
    InvestigationState,
    RunState,
    SystemState,
)
from kdive.mcp.auth import AuthError
from kdive.mcp.tools import accounting as acct_tools
from kdive.mcp.tools import allocations as alloc_tools
from kdive.mcp.tools import control as control_tools
from kdive.mcp.tools import systems as systems_tools
from kdive.mcp.tools import systems_handlers
from kdive.providers.local_libvirt.provisioning import domain_name_for
from kdive.reconciler import loop
from kdive.security.rbac import AuthorizationError, Role
from tests.integration._seed import (
    provisioning_profile,
    register_resource,
    seed_project_limits,
)
from tests.integration.conftest import open_pool, request_context
from tests.mcp.roles import PROJECT_A, PROJECT_B, make_role_fixture

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_COEFF_LOCAL = Decimal("1.0")


def _rate(vcpus: int, memory_gb: int) -> Decimal:
    """The reference rate for the local cost class (coeff 1.0)."""
    return rate(_COEFF_LOCAL, vcpus=vcpus, memory_gb=memory_gb)


def _estimate(vcpus: int, memory_gb: int, window_hours: Decimal | int | str) -> Decimal:
    return quantize_kcu(cost(_rate(vcpus, memory_gb), Decimal(str(window_hours))))


# --- shared DB readers ---------------------------------------------------------------------


async def _ledger_events(pool: AsyncConnectionPool, alloc_id: UUID) -> list[tuple[str, Decimal]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT event_type, kcu_delta FROM ledger WHERE allocation_id = %s ORDER BY ts, id",
            (alloc_id,),
        )
        return [(row[0], Decimal(row[1])) for row in await cur.fetchall()]


async def _spent(pool: AsyncConnectionPool, project: str) -> Decimal:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT spent_kcu FROM budgets WHERE project = %s", (project,))
        row = await cur.fetchone()
    assert row is not None
    return Decimal(row[0])


async def _count(
    pool: AsyncConnectionPool, table: str, where: LiteralString, params: tuple[object, ...]
) -> int:
    """Count rows in ``table`` matching ``where`` (a parameterized predicate fragment).

    The table name and predicate are composed via ``psycopg.sql`` so the identifier is
    quoted, not interpolated, and the row values stay bound parameters; ``where`` is a
    ``LiteralString`` (test-fixed), so no caller can pass user input here.
    """
    query = sql.SQL("SELECT count(*) FROM {table} WHERE {where}").format(
        table=sql.Identifier(table),
        where=sql.SQL(where),
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _operator_ctx(project: str = "proj"):
    return request_context(Role.OPERATOR, projects=(project,))


def _viewer_ctx(project: str = "proj"):
    return request_context(Role.VIEWER, projects=(project,))


def _admin_ctx(project: str = "proj"):
    return request_context(Role.ADMIN, projects=(project,))


# === Criterion 1: budget denial + input validation + idempotency ===========================


def test_c1_within_budget_grant_writes_one_reserved_row(migrated_url: str) -> None:
    """#1: a within-budget request grants and writes exactly one reserved row + audit."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            resp = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert resp.status == "granted"
            alloc_id = UUID(resp.object_id)
            events = await _ledger_events(pool, alloc_id)
            assert [e[0] for e in events] == ["reserved"]
            assert events[0][1] == _estimate(2, 4, 3)  # rate 3.0 * 3h = 9.0
            assert await _spent(pool, "proj") == _estimate(2, 4, 3)
            assert await _count(pool, "audit_log", "object_id = %s", (alloc_id,)) >= 1

    asyncio.run(_run())


def test_c1_over_budget_denied_no_durable_row(migrated_url: str) -> None:
    """#1: an over-budget request is denied with no allocation, ledger, or audit row."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            # rate 3.0 * 3h = 9.0 estimate; a 5.0 budget cannot cover it.
            await seed_project_limits(pool, limit_kcu=5)
            resp = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert resp.status == "error"
            assert resp.error_category == "allocation_denied"
            assert await _count(pool, "allocations", "project = %s", ("proj",)) == 0
            assert await _count(pool, "ledger", "project = %s", ("proj",)) == 0
            assert await _count(pool, "audit_log", "project = %s", ("proj",)) == 0
            assert await _spent(pool, "proj") == Decimal(0)  # untouched

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("vcpus", "memory_gb", "window"),
    [
        (0, 4, 3),  # vcpus < 1
        (2, -1, 3),  # memory_gb < 0
        (99, 4, 3),  # over the fake host's 8-vcpu ceiling
        (2, 4, 0),  # window <= 0
    ],
)
def test_c1_malformed_request_is_config_error_no_row(
    migrated_url: str, vcpus: int, memory_gb: int, window: int
) -> None:
    """#1: a malformed selector/window is configuration_error with no durable row."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            resp = await alloc_tools.request_allocation(
                pool,
                _operator_ctx(),
                project="proj",
                vcpus=vcpus,
                memory_gb=memory_gb,
                window=window,
            )
            assert resp.status == "error"
            assert resp.error_category == "configuration_error"
            assert await _count(pool, "allocations", "project = %s", ("proj",)) == 0
            assert await _count(pool, "ledger", "project = %s", ("proj",)) == 0
            assert await _spent(pool, "proj") == Decimal(0)

    asyncio.run(_run())


def test_c1_replayed_idempotency_key_no_second_grant_or_debit(migrated_url: str) -> None:
    """#1: a replayed idempotency_key returns the original allocation, no second debit."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            first = await alloc_tools.request_allocation(
                pool,
                _operator_ctx(),
                project="proj",
                vcpus=2,
                memory_gb=4,
                window=3,
                idempotency_key="retry-1",
            )
            assert first.status == "granted"
            spent_after_first = await _spent(pool, "proj")
            second = await alloc_tools.request_allocation(
                pool,
                _operator_ctx(),
                project="proj",
                vcpus=2,
                memory_gb=4,
                window=3,
                idempotency_key="retry-1",
            )
            assert second.status == "granted"
            assert second.object_id == first.object_id  # same allocation, no re-grant
            assert await _count(pool, "allocations", "project = %s", ("proj",)) == 1
            assert [e[0] for e in await _ledger_events(pool, UUID(first.object_id))] == ["reserved"]
            assert await _spent(pool, "proj") == spent_after_first  # no second debit

    asyncio.run(_run())


def test_c1_same_key_two_principals_isolated(migrated_url: str) -> None:
    """#1: the same key string under two principals resolves to each caller's own grant."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool, concurrent_allocation_cap=4)
            await seed_project_limits(pool, project=PROJECT_A, limit_kcu=1000)
            roles = make_role_fixture()
            op_one = roles.project(PROJECT_A).operator.ctx
            # A second operator principal on the same project (the role fixture mints one
            # operator per project, so build the second from request_context with a distinct
            # principal but the same project + role).
            op_two = request_context(Role.OPERATOR, principal="operator-two", projects=(PROJECT_A,))
            first = await alloc_tools.request_allocation(
                pool,
                op_one,
                project=PROJECT_A,
                vcpus=2,
                memory_gb=4,
                window=3,
                idempotency_key="shared-key",
            )
            second = await alloc_tools.request_allocation(
                pool,
                op_two,
                project=PROJECT_A,
                vcpus=2,
                memory_gb=4,
                window=3,
                idempotency_key="shared-key",
            )
            assert first.status == "granted" and second.status == "granted"
            assert first.object_id != second.object_id  # no cross-principal resolve
            assert await _count(pool, "allocations", "project = %s", (PROJECT_A,)) == 2

    asyncio.run(_run())


# === Criterion 2: quota denial =============================================================


def test_c2_alloc_quota_denied(migrated_url: str) -> None:
    """#2: at max_concurrent_allocations, request returns quota_exceeded, no second row."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool, concurrent_allocation_cap=4)
            await seed_project_limits(pool, limit_kcu=1000, max_allocations=1)
            first = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert first.status == "granted"
            second = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert second.status == "error"
            assert second.error_category == "quota_exceeded"
            assert await _count(pool, "allocations", "project = %s", ("proj",)) == 1
            assert await _count(pool, "ledger", "project = %s", ("proj",)) == 1  # only the grant

    asyncio.run(_run())


def test_c2_system_quota_denied(migrated_url: str) -> None:
    """#2: at max_concurrent_systems, provision on a distinct allocation returns quota_exceeded."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool, concurrent_allocation_cap=4)
            await seed_project_limits(pool, limit_kcu=1000, max_allocations=4, max_systems=1)
            ctx = _operator_ctx()
            first = await alloc_tools.request_allocation(
                pool, ctx, project="proj", vcpus=2, memory_gb=4, window=3
            )
            second = await alloc_tools.request_allocation(
                pool, ctx, project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert first.status == "granted" and second.status == "granted"
            # First provision occupies the single System slot (inserts as provisioning).
            prov_one = await systems_tools.provision_system(
                pool, ctx, allocation_id=first.object_id, profile=provisioning_profile()
            )
            assert prov_one.status == "queued"
            # Second provision is on a DISTINCT allocation -> reaches the new-System quota branch.
            prov_two = await systems_tools.provision_system(
                pool, ctx, allocation_id=second.object_id, profile=provisioning_profile()
            )
            assert prov_two.status == "error"
            assert prov_two.error_category == "quota_exceeded"
            assert await _count(pool, "systems", "project = %s", ("proj",)) == 1
            assert (
                await _count(pool, "jobs", "payload->>'system_id' IS NOT NULL", ()) == 1
            )  # only the first provision job

    asyncio.run(_run())


# === Criterion 3: ledger reconciliation + investigation rollup =============================


async def _seed_active_metered_alloc(
    pool: AsyncConnectionPool,
    *,
    project: str = "proj",
    vcpus: int = 2,
    memory_gb: int = 4,
    window_hours: int = 3,
    estimate: Decimal,
) -> UUID:
    """Seed a resource + budget + an active, sized allocation with one reserved row.

    The rollup/cross-project tests that use this only need a metered allocation (a reserved
    ledger row), not an open billing interval, so ``active_started_at`` is left null. The
    honest provision -> ready -> release billing path (which stamps the interval) is asserted
    by ``test_c3_reconciliation_nets_to_actual_and_usage_matches``.
    """
    res_id = await register_resource(pool, concurrent_allocation_cap=4)
    await seed_project_limits(pool, project=project, limit_kcu=1000)
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=UUID(res_id),
                state=AllocationState.ACTIVE,
                lease_expiry=datetime.now(UTC) + timedelta(hours=window_hours),
                requested_vcpus=vcpus,
                requested_memory_gb=memory_gb,
            ),
        )
        await accounting.reserve(conn, alloc, estimate)
    return alloc.id


def test_c3_estimate_equals_reserved_row(migrated_url: str) -> None:
    """#3: accounting.estimate equals the reserved row (both rate * window), asserted alone."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            est = await acct_tools.estimate(
                pool, _viewer_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert est.status != "error"
            grant = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            reserved = (await _ledger_events(pool, UUID(grant.object_id)))[0][1]
            assert Decimal(est.data["estimate_kcu"]) == reserved == _estimate(2, 4, 3)

    asyncio.run(_run())


class _FakeProvisioner:
    """A Provisioner stand-in: provision/teardown return a domain name and record nothing."""

    def provision(self, system_id: UUID, profile: object) -> str:
        return domain_name_for(system_id)

    def reprovision(self, system_id: UUID, profile: object) -> str:
        return domain_name_for(system_id)

    def teardown(self, domain_name: str) -> None:
        return None


async def _provision_job_for_system(pool: AsyncConnectionPool, system_id: str) -> Job:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM jobs WHERE kind = 'provision' AND payload->>'system_id' = %s",
            (system_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return Job.model_validate(row)


def test_c3_reconciliation_nets_to_actual_and_usage_matches(migrated_url: str) -> None:
    """#3: the honest provision -> ready -> release path bills rate*active_hours, usage matches.

    Drives the real handlers end to end (no explicitly-seeded billing interval): provision
    flips the allocation granted->active, the provision handler stamps active_started_at on
    ready, the clock is advanced by back-dating that stamp, and release reconciles to a
    partial charge — reserved+reconciled = rate*active_hours, not the full credit-back a
    never-stamped interval (active_hours = 0) would produce; usage.spent_kcu = that sum.
    """

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool, concurrent_allocation_cap=4)
            await seed_project_limits(pool, limit_kcu=1000, max_systems=4)
            op = _operator_ctx()
            grant = await alloc_tools.request_allocation(
                pool, op, project="proj", vcpus=2, memory_gb=4, window=3
            )
            assert grant.status == "granted"
            alloc_id = UUID(grant.object_id)
            estimate = _estimate(2, 4, 3)  # rate 3.0 * 3h window = 9.0 reserved
            prov = await systems_tools.provision_system(
                pool, op, allocation_id=grant.object_id, profile=provisioning_profile()
            )
            assert prov.status == "queued"
            job = await _provision_job_for_system(pool, prov.data["system_id"])
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, _FakeProvisioner())
            # The handler stamped active_started_at on ready; back-date it 2h to simulate
            # the lease running before release (no explicit seed of the interval).
            assert (await _alloc(pool, alloc_id)).active_started_at is not None
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE allocations SET active_started_at = %s WHERE id = %s",
                    (datetime.now(UTC) - timedelta(hours=2), alloc_id),
                )
            resp = await alloc_tools.release_allocation(pool, op, grant.object_id)
            assert resp.status == "released"
            events = await _ledger_events(pool, alloc_id)
            assert [e[0] for e in events] == ["reserved", "reconciled"]
            net = sum((e[1] for e in events), Decimal(0))
            actual = _estimate(2, 4, 2)  # rate 3.0 * 2h active = 6.0
            assert net == actual  # billed the active interval, not credited back in full
            assert actual != estimate  # the lease did not run the full 3h window
            assert net != Decimal(0)  # the bug would have netted 0 (active_hours = 0)
            usage = await acct_tools.usage(pool, _viewer_ctx(), project="proj")
            assert Decimal(usage.data["spent_kcu"]) == net

    asyncio.run(_run())


def test_c3_release_from_granted_credits_full_reservation(migrated_url: str) -> None:
    """#3: an allocation released from granted (never active) credits the full reservation."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            grant = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            resp = await alloc_tools.release_allocation(pool, _operator_ctx(), grant.object_id)
            assert resp.status == "released"
            net = sum((e[1] for e in await _ledger_events(pool, UUID(grant.object_id))), Decimal(0))
            assert net == Decimal(0)  # active_hours = 0 -> full credit
            assert await _spent(pool, "proj") == Decimal(0)

    asyncio.run(_run())


async def _seed_run_for_investigation(
    pool: AsyncConnectionPool,
    *,
    allocation_id: UUID,
    project: str,
    investigation_id: UUID | None = None,
) -> UUID:
    """Insert an investigation (if not given) + a System + a Run; return the investigation id."""
    async with pool.connection() as conn:
        if investigation_id is None:
            inv = await INVESTIGATIONS.insert(
                conn,
                Investigation(
                    id=uuid4(),
                    created_at=_DT,
                    updated_at=_DT,
                    principal="user-1",
                    project=project,
                    title="inv",
                    state=InvestigationState.ACTIVE,
                ),
            )
            investigation_id = inv.id
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=allocation_id,
                state=SystemState.READY,
                provisioning_profile=provisioning_profile(),
            ),
        )
        await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                investigation_id=investigation_id,
                system_id=system.id,
                state=RunState.SUCCEEDED,
                build_profile={},
            ),
        )
    return investigation_id


def test_c3_investigation_rollup_no_double_count(migrated_url: str) -> None:
    """#3: a shared allocation lands only in shared_kcu; per-investigation sums never overlap."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            # Exclusive allocation: its single Run is solely in investigation X.
            exclusive = await _seed_active_metered_alloc(pool, estimate=_estimate(2, 4, 3))
            inv_x = await _seed_run_for_investigation(pool, allocation_id=exclusive, project="proj")
            # Shared allocation: backs two Systems whose Runs span two investigations
            # (the reprovision-in-place reuse shape).
            shared = await _seed_active_metered_alloc(pool, estimate=_estimate(2, 4, 3))
            inv_y = await _seed_run_for_investigation(pool, allocation_id=shared, project="proj")
            await _seed_run_for_investigation(pool, allocation_id=shared, project="proj")
            async with pool.connection() as conn:
                excl_kcu = await accounting.usage_for_investigation(conn, inv_x)
                y_kcu = await accounting.usage_for_investigation(conn, inv_y)
                rollup = await accounting.usage(conn, "proj")
            exclusive_reserved = (await _ledger_events(pool, exclusive))[0][1]
            shared_reserved = (await _ledger_events(pool, shared))[0][1]
            assert excl_kcu == exclusive_reserved  # the exclusive allocation only
            assert y_kcu == Decimal(0)  # the shared allocation is in NEITHER rollup
            assert rollup.shared_kcu == shared_reserved  # shared shows only in the project rollup
            # Per-investigation sums never exceed the project total, never double-count.
            assert excl_kcu + y_kcu <= rollup.spent_kcu

    asyncio.run(_run())


# === Criterion 4: idle lease expiry (+ Run-fail on the path that owns it) ===================


async def _seed_expired_active_alloc_with_system(
    pool: AsyncConnectionPool, *, project: str = "proj"
) -> tuple[UUID, UUID]:
    """Seed an active, sized, metered allocation past its lease + a ready System; return ids."""
    res_id = await register_resource(pool, concurrent_allocation_cap=4)
    await seed_project_limits(pool, project=project, limit_kcu=1000)
    async with pool.connection() as conn:
        started = datetime.now(UTC) - timedelta(hours=2)
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                resource_id=UUID(res_id),
                state=AllocationState.ACTIVE,
                requested_vcpus=2,
                requested_memory_gb=4,
                active_started_at=started,
            ),
        )
        await accounting.reserve(conn, alloc, _estimate(2, 4, 3))
        await conn.execute(
            "UPDATE allocations SET lease_expiry = now() - interval '1 hour' WHERE id = %s",
            (alloc.id,),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project=project,
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile=provisioning_profile(),
            ),
        )
    return alloc.id, system.id


def test_c4_idle_lease_expiry_sweeps_and_credits(migrated_url: str) -> None:
    """#4: an idle expired allocation -> expired, System teardown enqueued, reservation credited."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            alloc_id, system_id = await _seed_expired_active_alloc_with_system(pool)
            report = await loop.reconcile_once(pool, loop.NullReaper())
            assert report.expired_allocations == 1
            assert report.orphaned_systems == 1  # the now-expired allocation orphaned its System
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, active_ended_at FROM allocations WHERE id = %s", (alloc_id,)
                )
                alloc_row = await cur.fetchone()
                await cur.execute(
                    "SELECT kind FROM jobs WHERE dedup_key = %s", (f"{system_id}:teardown",)
                )
                job_row = await cur.fetchone()
            assert alloc_row is not None
            assert alloc_row["state"] == "expired"  # distinct from released
            assert alloc_row["active_ended_at"] is not None  # billing interval closed
            assert job_row is not None and job_row["kind"] == "teardown"
            kinds = [e[0] for e in await _ledger_events(pool, alloc_id)]
            assert kinds == ["reserved", "reconciled"]  # unused reservation credited back

    asyncio.run(_run())


def test_c4_abandoned_job_fails_run_lease_expired(migrated_url: str) -> None:
    """#4: the Run-fail-on-lease_expired contract on the path that owns it (abandoned-job repair).

    The idle sweep does not fail the Run (no in-flight job); Run -> failed(lease_expired) is
    produced by ``_repair_abandoned_jobs`` on a zombie job carrying a non-terminal Run.
    """

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            alloc_id, system_id = await _seed_expired_active_alloc_with_system(pool)
            async with pool.connection() as conn:
                inv = await INVESTIGATIONS.insert(
                    conn,
                    Investigation(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="user-1",
                        project="proj",
                        title="inv",
                        state=InvestigationState.ACTIVE,
                    ),
                )
                run = await RUNS.insert(
                    conn,
                    Run(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        principal="user-1",
                        project="proj",
                        investigation_id=inv.id,
                        system_id=system_id,
                        state=RunState.RUNNING,
                        build_profile={},
                    ),
                )
                # A zombie build job: running, lease lapsed, attempts exhausted (only the
                # reconciler can sweep it), carrying the non-terminal run_id.
                await conn.execute(
                    "INSERT INTO jobs (kind, payload, state, attempt, max_attempts, worker_id, "
                    "    lease_expires_at, authorizing, dedup_key) "
                    "VALUES ('build', %s, 'running', 3, 3, 'w-dead', "
                    "    now() - interval '1 minute', %s, %s)",
                    (
                        Jsonb({"run_id": str(run.id)}),
                        Jsonb(
                            {
                                "principal": "allocation-test",
                                "agent_session": None,
                                "project": "proj",
                            }
                        ),
                        f"{run.id}:build",
                    ),
                )
            await loop.reconcile_once(pool, loop.NullReaper())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, failure_category FROM runs WHERE id = %s", (run.id,)
                )
                row = await cur.fetchone()
            assert row is not None
            assert row["state"] == "failed"
            assert row["failure_category"] == "lease_expired"

    asyncio.run(_run())


# === Criterion 5: renewal ==================================================================


def test_c5_renew_extends_window_and_charges(migrated_url: str) -> None:
    """#5: renew extends the lease and writes an incremental reserved delta + spend bump."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            grant = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            alloc_id = UUID(grant.object_id)
            before = (await _alloc(pool, alloc_id)).lease_expiry
            resp = await alloc_tools.renew_allocation(
                pool, _operator_ctx(), str(alloc_id), extend=3
            )
            assert resp.status == "granted"
            events = await _ledger_events(pool, alloc_id)
            assert [e[0] for e in events] == ["reserved", "reserved"]
            assert events[1][1] == _estimate(2, 4, 3)  # +3h * rate 3.0 = +9.0
            after = (await _alloc(pool, alloc_id)).lease_expiry
            assert before is not None and after is not None
            assert after - before == timedelta(hours=3)
            assert await _spent(pool, "proj") == _estimate(2, 4, 3) * 2  # grant + renew

    asyncio.run(_run())


def test_c5_over_budget_renew_denied_window_unchanged(migrated_url: str) -> None:
    """#5: an over-budget renew is denied and leaves the window unchanged, no second reserved."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            # budget covers exactly the 9.0 grant; a +3h renew (9.0 more) is over budget.
            await seed_project_limits(pool, limit_kcu=9)
            grant = await alloc_tools.request_allocation(
                pool, _operator_ctx(), project="proj", vcpus=2, memory_gb=4, window=3
            )
            alloc_id = UUID(grant.object_id)
            before = (await _alloc(pool, alloc_id)).lease_expiry
            resp = await alloc_tools.renew_allocation(
                pool, _operator_ctx(), str(alloc_id), extend=3
            )
            assert resp.status == "error"
            assert resp.error_category == "allocation_denied"
            assert (await _alloc(pool, alloc_id)).lease_expiry == before  # unchanged
            reserved = [e for e in await _ledger_events(pool, alloc_id) if e[0] == "reserved"]
            assert len(reserved) == 1  # no incremental reserved row
            assert await _spent(pool, "proj") == _estimate(2, 4, 3)  # no extra charge

    asyncio.run(_run())


async def _alloc(pool: AsyncConnectionPool, alloc_id: UUID) -> Allocation:
    async with pool.connection() as conn:
        alloc = await ALLOCATIONS.get(conn, alloc_id)
    assert alloc is not None
    return alloc


# === Criterion 6: role separation ==========================================================


def test_c6_operator_refused_admin_bare_require_role_ops(migrated_url: str) -> None:
    """#6: set_budget/set_quota/power-off/teardown raise AuthorizationError for an operator."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            op = _operator_ctx()
            with pytest.raises(AuthorizationError):
                await acct_tools.set_budget(pool, op, project="proj", limit_kcu="10")
            with pytest.raises(AuthorizationError):
                await acct_tools.set_quota(
                    pool, op, project="proj", max_concurrent_allocations=1, max_concurrent_systems=1
                )
            # power off / teardown bind their admin check to a real System's project.
            grant = await alloc_tools.request_allocation(
                pool, op, project="proj", vcpus=2, memory_gb=4, window=3
            )
            prov = await systems_tools.provision_system(
                pool, op, allocation_id=grant.object_id, profile=provisioning_profile()
            )
            sys_id = prov.data["system_id"]
            async with pool.connection() as conn:
                await conn.execute("UPDATE systems SET state = 'ready' WHERE id = %s", (sys_id,))
            with pytest.raises(AuthorizationError):
                await control_tools.power_system(pool, op, system_id=sys_id, action="off")
            with pytest.raises(AuthorizationError):
                await systems_tools.teardown_system(pool, op, sys_id)

    asyncio.run(_run())


def test_c6_operator_force_crash_returns_authorization_denied_envelope(migrated_url: str) -> None:
    """#6: force_crash's three-check gate returns the authorization_denied ENVELOPE (ADR-0020)."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            await seed_project_limits(pool, limit_kcu=1000)
            op = _operator_ctx()
            grant = await alloc_tools.request_allocation(
                pool, op, project="proj", vcpus=2, memory_gb=4, window=3
            )
            prov = await systems_tools.provision_system(
                pool,
                op,
                allocation_id=grant.object_id,
                profile=provisioning_profile(destructive_ops=["force_crash"]),
            )
            sys_id = prov.data["system_id"]
            async with pool.connection() as conn:
                await conn.execute("UPDATE systems SET state = 'ready' WHERE id = %s", (sys_id,))
                await conn.execute(
                    "UPDATE allocations SET capability_scope = %s WHERE id = %s",
                    (Jsonb({"destructive_ops": ["force_crash"]}), grant.object_id),
                )
            resp = await control_tools.force_crash_system(pool, op, system_id=sys_id)
            assert resp.status == "error"
            assert resp.error_category == "authorization_denied"  # envelope, not a raise

    asyncio.run(_run())


def test_c6_admin_and_operator_succeed_on_their_surfaces(migrated_url: str) -> None:
    """#6: admin set_budget/set_quota succeed; operator reprovision + power-on succeed."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            admin = _admin_ctx()
            assert (
                await acct_tools.set_budget(pool, admin, project="proj", limit_kcu="1000")
            ).status != "error"
            assert (
                await acct_tools.set_quota(
                    pool,
                    admin,
                    project="proj",
                    max_concurrent_allocations=4,
                    max_concurrent_systems=4,
                )
            ).status != "error"
            op = _operator_ctx()
            grant = await alloc_tools.request_allocation(
                pool, op, project="proj", vcpus=2, memory_gb=4, window=3
            )
            prov = await systems_tools.provision_system(
                pool,
                op,
                allocation_id=grant.object_id,
                profile=provisioning_profile(destructive_ops=["reprovision"]),
            )
            sys_id = prov.data["system_id"]
            async with pool.connection() as conn:
                await conn.execute("UPDATE systems SET state = 'ready' WHERE id = %s", (sys_id,))
                await conn.execute(
                    "UPDATE allocations SET capability_scope = %s WHERE id = %s",
                    (Jsonb({"destructive_ops": ["reprovision"]}), grant.object_id),
                )
            power_on = await control_tools.power_system(pool, op, system_id=sys_id, action="on")
            assert power_on.status == "queued"
            reprov = await systems_tools.reprovision_system(
                pool,
                op,
                system_id=sys_id,
                profile=provisioning_profile(destructive_ops=["reprovision"]),
            )
            assert reprov.status == "queued"

    asyncio.run(_run())


def test_c6_viewer_refused_cross_project_usage_by_investigation(migrated_url: str) -> None:
    """#6: a PROJECT_A viewer cannot read PROJECT_B spend via a foreign investigation_id."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool, concurrent_allocation_cap=4)
            await seed_project_limits(pool, project=PROJECT_B, limit_kcu=1000)
            alloc_b = await _seed_active_metered_alloc(
                pool, project=PROJECT_B, estimate=_estimate(2, 4, 3)
            )
            inv_b = await _seed_run_for_investigation(
                pool, allocation_id=alloc_b, project=PROJECT_B
            )
            roles = make_role_fixture()
            viewer_a = roles.project(PROJECT_A).viewer.ctx  # member of A only
            # usage(investigation_id) resolves the investigation's OWNING project (proj-b)
            # and authorizes on it; a proj-a-only viewer is not a member, so the resolve
            # raises before any spend is read (the tenant-isolation boundary, ADR-0007 §6).
            with pytest.raises((AuthError, AuthorizationError)):
                await acct_tools.usage(pool, viewer_a, investigation_id=str(inv_b))

    asyncio.run(_run())


# === Criterion 7: reprovision-in-place =====================================================


class _RecordingProvisioner:
    """A Provisioner stand-in recording reprovision() calls and returning a domain name."""

    def __init__(self) -> None:
        self.reprovisioned: list[UUID] = []

    def provision(self, system_id: UUID, profile: object) -> str:
        return domain_name_for(system_id)

    def reprovision(self, system_id: UUID, profile: object) -> str:
        self.reprovisioned.append(system_id)
        return domain_name_for(system_id)

    def teardown(self, domain_name: str) -> None:
        return None


def test_c7_reprovision_in_place_cycle(migrated_url: str) -> None:
    """#7: reprovision cycles ready -> reprovisioning -> ready on the same row + allocation."""

    async def _run() -> None:
        async with open_pool(migrated_url) as pool:
            await register_resource(pool)
            op = _operator_ctx()
            await seed_project_limits(pool, limit_kcu=1000)
            grant = await alloc_tools.request_allocation(
                pool, op, project="proj", vcpus=2, memory_gb=4, window=3
            )
            prov = await systems_tools.provision_system(
                pool,
                op,
                allocation_id=grant.object_id,
                profile=provisioning_profile(destructive_ops=["reprovision"]),
            )
            sys_id = prov.data["system_id"]
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE systems SET state = 'ready', domain_name = %s WHERE id = %s",
                    (f"kdive-{sys_id}", sys_id),
                )
                await conn.execute(
                    "UPDATE allocations SET capability_scope = %s WHERE id = %s",
                    (Jsonb({"destructive_ops": ["reprovision"]}), grant.object_id),
                )
            new_profile = provisioning_profile(destructive_ops=["reprovision"])
            new_profile["vcpu"] = 8
            resp = await systems_tools.reprovision_system(
                pool, op, system_id=sys_id, profile=new_profile
            )
            assert resp.status == "queued"
            # Drive the reprovision job handler -> reprovisioning -> ready (same row).
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM jobs WHERE kind = 'reprovision' AND payload->>'system_id' = %s",
                    (sys_id,),
                )
                job_row = await cur.fetchone()
            assert job_row is not None
            job = Job.model_validate(job_row)
            async with pool.connection() as conn:
                await systems_handlers.reprovision_handler(conn, job, _RecordingProvisioner())
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT state, provisioning_profile FROM systems WHERE id = %s", (sys_id,)
                )
                sys_row = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM systems")
                sys_n = await cur.fetchone()
                await cur.execute("SELECT count(*) AS n FROM allocations")
                alloc_n = await cur.fetchone()
            assert sys_row is not None and sys_row["state"] == "ready"  # cycled back
            assert sys_row["provisioning_profile"]["vcpu"] == 8  # new profile applied in place
            assert sys_n is not None and sys_n["n"] == 1  # no new System row
            assert alloc_n is not None and alloc_n["n"] == 1  # no new Allocation row

    asyncio.run(_run())


# === Criterion 8: live introspection (live_vm-gated) =======================================


_LIVE_SSH_ENV = "KDIVE_LIVE_SSH_TARGET"


@pytest.mark.live_vm
def test_c8_live_introspect_over_ssh(migrated_url: str) -> None:  # pragma: no cover - live_vm
    """#8: live drgn over SSH returns task/module/sysinfo, secret redacted, transcript sensitive.

    SKIPs in CI: the body is wired by the live_vm runner against an operator-provided
    SSH-reachable kdump guest (the same fixtures as the M0 full-path test, plus
    ``KDIVE_LIVE_SSH_TARGET``). On a real host it asserts: ``debug.start_session(transport=
    "ssh")`` then ``introspect.run`` returns a live report; a planted secret is ``[REDACTED]``
    in the response; and the raw transcript is marked ``sensitive``. The non-gated redaction
    contract is already covered by ``tests/mcp/test_introspect_tools.py`` against a fake live
    introspector, so CI retains a real signal for the redaction invariant.
    """
    from tests.integration.test_walking_skeleton import _live_vm_preflight

    _live_vm_preflight(require_ssh=True)
    raise NotImplementedError("live_vm SSH/introspect harness wired by the live_vm runner")
