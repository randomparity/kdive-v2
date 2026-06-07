"""Tests for the typed async repositories (ADR-0003, ADR-0016)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import (
    ALLOCATIONS,
    ARTIFACTS,
    BUDGETS,
    COST_CLASS_COEFFICIENTS,
    DEBUG_SESSIONS,
    INVESTIGATIONS,
    JOBS,
    LEDGER,
    QUOTAS,
    RESOURCES,
    RUNS,
    SYSTEMS,
    ObjectNotFound,
)
from kdive.domain.models import (
    Allocation,
    Artifact,
    Budget,
    DebugSession,
    ExternalRef,
    Investigation,
    Job,
    JobKind,
    LedgerEntry,
    LedgerEventType,
    Quota,
    Resource,
    ResourceKind,
    Run,
    Sensitivity,
    System,
)
from kdive.domain.state import (
    AllocationState,
    DebugSessionState,
    IllegalTransition,
    InvestigationState,
    JobState,
    ResourceStatus,
    RunState,
    SystemState,
)

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _resource(**kw: object) -> Resource:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        pool="p",
        cost_class="c",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
    )
    base.update(kw)
    return Resource.model_validate(base)


def _allocation(resource_id: UUID, **kw: object) -> Allocation:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        resource_id=resource_id,
        state=AllocationState.REQUESTED,
    )
    base.update(kw)
    return Allocation.model_validate(base)


def _system(allocation_id: UUID, **kw: object) -> System:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        allocation_id=allocation_id,
        state=SystemState.DEFINED,
        provisioning_profile={"k": "v"},
    )
    base.update(kw)
    return System.model_validate(base)


def _investigation(**kw: object) -> Investigation:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        title="t",
        state=InvestigationState.OPEN,
    )
    base.update(kw)
    return Investigation.model_validate(base)


def _run(investigation_id: UUID, system_id: UUID, **kw: object) -> Run:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        investigation_id=investigation_id,
        system_id=system_id,
        state=RunState.CREATED,
        build_profile={"cfg": 1},
    )
    base.update(kw)
    return Run.model_validate(base)


def _debug_session(run_id: UUID, **kw: object) -> DebugSession:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="alice",
        project="proj",
        run_id=run_id,
        state=DebugSessionState.ATTACH,
        transport="gdb",
    )
    base.update(kw)
    return DebugSession.model_validate(base)


def _job(**kw: object) -> Job:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=JobKind.BUILD,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key=str(uuid4()),
    )
    base.update(kw)
    return Job.model_validate(base)


def _artifact(owner_id: UUID, **kw: object) -> Artifact:
    base: dict[str, object] = dict(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        owner_kind="system",
        owner_id=owner_id,
        object_key="k",
        etag="e",
        sensitivity=Sensitivity.REDACTED,
        retention_class="default",
    )
    base.update(kw)
    return Artifact.model_validate(base)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_roundtrip_every_object(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource(capabilities={"kvm": True}))
            assert await RESOURCES.get(conn, res.id) == res

            alloc = await ALLOCATIONS.insert(
                conn, _allocation(res.id, capability_scope={"cpus": 4})
            )
            assert await ALLOCATIONS.get(conn, alloc.id) == alloc

            sysm = await SYSTEMS.insert(conn, _system(alloc.id))
            assert await SYSTEMS.get(conn, sysm.id) == sysm

            inv = await INVESTIGATIONS.insert(
                conn,
                _investigation(external_refs=[ExternalRef(tracker="bz", id="1", url="http://x")]),
            )
            assert await INVESTIGATIONS.get(conn, inv.id) == inv

            run = await RUNS.insert(conn, _run(inv.id, sysm.id))
            assert await RUNS.get(conn, run.id) == run

            ds = await DEBUG_SESSIONS.insert(conn, _debug_session(run.id))
            assert await DEBUG_SESSIONS.get(conn, ds.id) == ds

            job = await JOBS.insert(conn, _job(payload={"x": 1}))
            assert await JOBS.get(conn, job.id) == job

            art = await ARTIFACTS.insert(conn, _artifact(sysm.id))
            assert await ARTIFACTS.get(conn, art.id) == art

    asyncio.run(_run_test())


def test_get_miss_returns_none(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            assert await RESOURCES.get(conn, uuid4()) is None

    asyncio.run(_run_test())


def test_insert_timestamps_are_db_authoritative(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            wrong = datetime(2000, 1, 1, tzinfo=UTC)
            res = _resource(created_at=wrong, updated_at=wrong)
            inserted = await RESOURCES.insert(conn, res)
            assert inserted.created_at != wrong
            assert inserted.created_at.year >= 2026

    asyncio.run(_run_test())


def test_update_state_legal_bumps_updated_at(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(conn, _allocation(res.id))
            updated = await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
            assert updated.state is AllocationState.GRANTED
            assert updated.updated_at > alloc.updated_at  # trigger bumped it

    asyncio.run(_run_test())


def test_update_state_illegal_raises(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(conn, _allocation(res.id))
            with pytest.raises(IllegalTransition):
                await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.RELEASED)

    asyncio.run(_run_test())


def test_update_state_unknown_id_raises(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(ObjectNotFound):
                await ALLOCATIONS.update_state(conn, uuid4(), AllocationState.GRANTED)

    asyncio.run(_run_test())


def test_update_state_concurrent_same_target(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as setup:
            res = await RESOURCES.insert(setup, _resource())
            alloc = await ALLOCATIONS.insert(setup, _allocation(res.id))
        async with (
            await _connect(migrated_url) as a,
            await _connect(migrated_url) as b,
        ):

            async def go(conn: psycopg.AsyncConnection) -> object:
                return await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)

            results = await asyncio.gather(go(a), go(b), return_exceptions=True)
        successes = [r for r in results if isinstance(r, Allocation)]
        failures = [r for r in results if isinstance(r, IllegalTransition)]
        assert len(successes) == 1
        assert len(failures) == 1

    asyncio.run(_run_test())


def test_seed_coefficient_is_readable(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            local = await COST_CLASS_COEFFICIENTS.get(conn, "local")
            assert local is not None
            assert local.coeff == Decimal("1.0")
            assert await COST_CLASS_COEFFICIENTS.get(conn, "missing") is None

    asyncio.run(_run_test())


def test_keyed_repository_rejects_unknown_key_column() -> None:
    from kdive.db.repositories import KeyedRepository

    with pytest.raises(ValueError, match="no field 'nope'"):
        KeyedRepository(Budget, "budgets", "nope")


def test_budget_upsert_inserts_then_updates(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            created = await BUDGETS.upsert(
                conn, Budget(project="p", limit_kcu=Decimal("100"), updated_at=_DT)
            )
            assert created.limit_kcu == Decimal("100")
            assert created.spent_kcu == Decimal("0")  # DB default
            # Upserting the same PK overwrites limit_kcu (admin re-set_budget).
            updated = await BUDGETS.upsert(
                conn, Budget(project="p", limit_kcu=Decimal("250"), updated_at=_DT)
            )
            assert updated.limit_kcu == Decimal("250")
            fetched = await BUDGETS.get(conn, "p")
            assert fetched is not None and fetched.limit_kcu == Decimal("250")

    asyncio.run(_run_test())


def test_quota_upsert_and_get(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            await QUOTAS.upsert(
                conn,
                Quota(
                    project="p",
                    max_concurrent_allocations=3,
                    max_concurrent_systems=5,
                    updated_at=_DT,
                ),
            )
            fetched = await QUOTAS.get(conn, "p")
            assert fetched is not None
            assert fetched.max_concurrent_allocations == 3
            assert fetched.max_concurrent_systems == 5
            assert await QUOTAS.get(conn, "absent") is None

    asyncio.run(_run_test())


def test_ledger_insert_and_get_signed_delta(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(
                conn, _allocation(res.id, state=AllocationState.GRANTED)
            )
            entry = LedgerEntry(
                id=uuid4(),
                ts=_DT,
                project="proj",
                allocation_id=alloc.id,
                resource_id=res.id,
                cost_class="local",
                event_type=LedgerEventType.RECONCILED,
                kcu_delta=Decimal("-3.5"),
                note=None,
            )
            inserted = await LEDGER.insert(conn, entry)
            assert inserted.kcu_delta == Decimal("-3.5")
            assert inserted.ts != _DT  # DB-authoritative `ts`
            fetched = await LEDGER.get(conn, inserted.id)
            assert fetched is not None and fetched.event_type is LedgerEventType.RECONCILED

    asyncio.run(_run_test())


def test_ledger_allows_null_resource_id(migrated_url: str) -> None:
    # A credit reconciling an allocation released before any System was provisioned.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            res = await RESOURCES.insert(conn, _resource())
            alloc = await ALLOCATIONS.insert(
                conn, _allocation(res.id, state=AllocationState.GRANTED)
            )
            entry = LedgerEntry(
                id=uuid4(),
                ts=_DT,
                project="proj",
                allocation_id=alloc.id,
                cost_class="local",
                event_type=LedgerEventType.RESERVED,
                kcu_delta=Decimal("4"),
            )
            inserted = await LEDGER.insert(conn, entry)
            assert inserted.resource_id is None

    asyncio.run(_run_test())


def test_json_columns_match_schema(migrated_url: str) -> None:
    repos = [RESOURCES, ALLOCATIONS, SYSTEMS, INVESTIGATIONS, RUNS, DEBUG_SESSIONS, JOBS, ARTIFACTS]

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            for repo in repos:
                cur = await conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s AND data_type = 'jsonb'",
                    (repo._table,),
                )
                actual = {row[0] for row in await cur.fetchall()}
                assert repo._json_columns == actual, (
                    f"{repo._table}: {repo._json_columns} != {actual}"
                )

    asyncio.run(_run_test())
