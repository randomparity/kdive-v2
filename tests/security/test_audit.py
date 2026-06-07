"""Tests for the append-only audit record (ADR-0006, ADR-0020)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.db.repositories import ALLOCATIONS, RESOURCES
from kdive.domain.models import Allocation, Resource, ResourceKind
from kdive.domain.state import AllocationState, ResourceStatus
from kdive.mcp.auth import AuthError, RequestContext
from kdive.security.audit import AuditEvent, args_digest, record

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> RequestContext:
    return RequestContext(principal="alice", agent_session="sess-1", projects=("proj",))


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _seed_allocation(conn: psycopg.AsyncConnection) -> Allocation:
    res = await RESOURCES.insert(
        conn,
        Resource.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="p",
                cost_class="c",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            )
        ),
    )
    return await ALLOCATIONS.insert(
        conn,
        Allocation.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=res.id,
                state=AllocationState.REQUESTED,
            )
        ),
    )


async def _count_audit(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_args_digest_is_order_independent() -> None:
    assert args_digest({"a": 1, "b": {"x": 1, "y": 2}}) == args_digest(
        {"b": {"y": 2, "x": 1}, "a": 1}
    )


def test_args_digest_differs_for_different_args() -> None:
    assert args_digest({"a": 1}) != args_digest({"a": 2})


def test_args_digest_does_not_contain_secret() -> None:
    secret = "hunter2-supersecret"
    assert secret not in args_digest({"password": secret})


def test_args_digest_uuid_datetime_pins_canonical_encoding() -> None:
    # `default=str` renders these scalars deterministically; pin the exact canonical
    # form so a change to the encoding (and thus the digest) is caught.
    u = UUID("12345678-1234-5678-1234-567812345678")
    when = datetime(2026, 1, 1, tzinfo=UTC)
    canonical = '{"id":"12345678-1234-5678-1234-567812345678","when":"2026-01-01 00:00:00+00:00"}'
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert args_digest({"id": u, "when": when}) == expected


def test_record_writes_one_row(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            obj_id = uuid4()
            audit_id = await record(
                conn,
                _ctx(),
                AuditEvent(
                    tool="systems.teardown",
                    object_kind="systems",
                    object_id=obj_id,
                    transition="ready->torn_down",
                    args={"system_id": str(obj_id)},
                    project="proj",
                ),
            )
            assert isinstance(audit_id, UUID)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, object_kind, "
                    "object_id, transition, args_digest FROM audit_log WHERE id = %s",
                    (audit_id,),
                )
                row = await cur.fetchone()
            assert row == (
                "alice",
                "sess-1",
                "proj",
                "systems.teardown",
                "systems",
                obj_id,
                "ready->torn_down",
                args_digest({"system_id": str(obj_id)}),
            )
            assert await _count_audit(conn) == 1

    asyncio.run(_run_test())


def test_record_persists_null_agent_session(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            ctx = RequestContext(principal="alice", agent_session=None, projects=("proj",))
            audit_id = await record(
                conn,
                ctx,
                AuditEvent(
                    tool="systems.teardown",
                    object_kind="systems",
                    object_id=uuid4(),
                    transition="ready->torn_down",
                    args={},
                    project="proj",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT agent_session FROM audit_log WHERE id = %s", (audit_id,))
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is None  # principal-only attribution persists as SQL NULL

    asyncio.run(_run_test())


def test_record_rejects_ungranted_project(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(AuthError):
                await record(
                    conn,
                    _ctx(),
                    AuditEvent(
                        tool="systems.teardown",
                        object_kind="systems",
                        object_id=uuid4(),
                        transition="ready->torn_down",
                        args={},
                        project="not-granted",
                    ),
                )
            assert await _count_audit(conn) == 0

    asyncio.run(_run_test())


def test_record_in_transition_transaction_is_atomic(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            alloc = await _seed_allocation(conn)
            async with conn.transaction():
                await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
                await record(
                    conn,
                    _ctx(),
                    AuditEvent(
                        tool="allocations.grant",
                        object_kind="allocations",
                        object_id=alloc.id,
                        transition="requested->granted",
                        args={},
                        project="proj",
                    ),
                )
            assert await _count_audit(conn) == 1  # exactly one row per transition
            updated = await ALLOCATIONS.get(conn, alloc.id)
            assert updated is not None and updated.state is AllocationState.GRANTED

    asyncio.run(_run_test())


def test_record_rolls_back_with_failed_transition(migrated_url: str) -> None:
    class _Boom(RuntimeError):
        pass

    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            alloc = await _seed_allocation(conn)
            with pytest.raises(_Boom):
                async with conn.transaction():
                    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.GRANTED)
                    await record(
                        conn,
                        _ctx(),
                        AuditEvent(
                            tool="allocations.grant",
                            object_kind="allocations",
                            object_id=alloc.id,
                            transition="requested->granted",
                            args={},
                            project="proj",
                        ),
                    )
                    raise _Boom  # abort the whole transaction
            assert await _count_audit(conn) == 0  # audit row rolled back with the transition
            still = await ALLOCATIONS.get(conn, alloc.id)
            assert still is not None and still.state is AllocationState.REQUESTED

    asyncio.run(_run_test())
