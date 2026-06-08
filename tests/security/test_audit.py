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
from kdive.security.audit import (
    AuditEvent,
    DenialEvent,
    args_digest,
    record,
    record_denial,
    record_system,
)

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


def test_record_system_attributes_original_agent_session(migrated_url: str) -> None:
    # The promotion sweep (#165) acts under the service identity but attributes the grant to
    # the queued allocation's ORIGINAL (principal, agent_session) so the backlog grant is
    # indistinguishable in audit from a synchronous one (ADR-0069 §4). record_system carries
    # the agent_session, unlike a reconciler teardown (which leaves it NULL).
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            obj_id = uuid4()
            audit_id = await record_system(
                conn,
                principal="alice",
                agent_session="orig-sess",
                event=AuditEvent(
                    tool="allocations.request",
                    object_kind="allocations",
                    object_id=obj_id,
                    transition="requested->granted",
                    args={"project": "proj"},
                    project="proj",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session FROM audit_log WHERE id = %s", (audit_id,)
                )
                row = await cur.fetchone()
            assert row == ("alice", "orig-sess")

    asyncio.run(_run_test())


def test_record_system_defaults_agent_session_to_null(migrated_url: str) -> None:
    # A reconciler teardown carries no agent_session — the default preserves NULL.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            audit_id = await record_system(
                conn,
                principal="system:reconciler",
                event=AuditEvent(
                    tool="reconciler.sweep_expired",
                    object_kind="allocations",
                    object_id=uuid4(),
                    transition="active->expired",
                    args={},
                    project="proj",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT agent_session FROM audit_log WHERE id = %s", (audit_id,))
                row = await cur.fetchone()
            assert row is not None and row[0] is None

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


def test_record_denial_writes_one_row_with_null_object(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            audit_id = await record_denial(
                conn,
                event=DenialEvent(
                    principal="alice",
                    agent_session="sess-1",
                    project="proj",
                    tool="allocations.release",
                    args={"allocation_id": "abc"},
                    reason="needs role 'operator'; holds 'viewer'",
                ),
            )
            assert isinstance(audit_id, UUID)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, agent_session, project, tool, object_kind, "
                    "object_id, transition, args_digest, reason FROM audit_log WHERE id = %s",
                    (audit_id,),
                )
                row = await cur.fetchone()
            assert row == (
                "alice",
                "sess-1",
                "proj",
                "allocations.release",
                None,  # object_kind NULL on a denial row
                None,  # object_id NULL on a denial row
                "denied",  # the reserved bare transition literal (tool column carries the tool)
                args_digest({"allocation_id": "abc"}),
                "needs role 'operator'; holds 'viewer'",
            )
            assert await _count_audit(conn) == 1

    asyncio.run(_run_test())


def test_record_denial_persists_null_agent_session(migrated_url: str) -> None:
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            audit_id = await record_denial(
                conn,
                event=DenialEvent(
                    principal="alice",
                    agent_session=None,
                    project="proj",
                    tool="allocations.release",
                    args={},
                    reason="denied",
                ),
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT agent_session FROM audit_log WHERE id = %s", (audit_id,))
                row = await cur.fetchone()
            assert row is not None and row[0] is None

    asyncio.run(_run_test())


def test_record_denial_is_not_membership_guarded(migrated_url: str) -> None:
    # Guard-exempt writer (record_system precedent): it takes no RequestContext and so
    # writes regardless of any granted set — the boundary already authenticated the actor.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            await record_denial(
                conn,
                event=DenialEvent(
                    principal="alice",
                    agent_session=None,
                    project="not-in-any-granted-set",
                    tool="allocations.release",
                    args={},
                    reason="denied",
                ),
            )
            assert await _count_audit(conn) == 1

    asyncio.run(_run_test())


def test_check_rejects_non_denied_row_with_null_object(migrated_url: str) -> None:
    # The CHECK is keyed on transition='denied': a real-transition row must keep its
    # object (the original invariant), so a NULL-object non-denied row is rejected.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO audit_log "
                        "(principal, project, tool, object_kind, object_id, "
                        " transition, args_digest) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        ("alice", "proj", "systems.teardown", None, None, "ready->torn_down", "d"),
                    )

    asyncio.run(_run_test())


def test_check_accepts_destructive_gate_denied_row_carrying_object(migrated_url: str) -> None:
    # The destructive-gate denial uses transition='{op}:denied' and ALWAYS carries the
    # object it gated — it satisfies the CHECK's object-present branch, no exemption.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            obj_id = uuid4()
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log "
                    "(principal, project, tool, object_kind, object_id, "
                    " transition, args_digest) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        "alice",
                        "proj",
                        "control.force_crash",
                        "systems",
                        obj_id,
                        "force_crash:denied",
                        "d",
                    ),
                )
                row = await cur.fetchone()
            assert row is not None
            assert await _count_audit(conn) == 1

    asyncio.run(_run_test())


def test_check_rejects_denial_row_only_when_object_partially_null(migrated_url: str) -> None:
    # A bare 'denied' row may omit BOTH object columns; the success path (every other
    # transition) is unchanged. Confirm a normal full-object success row still inserts.
    async def _run_test() -> None:
        async with await _connect(migrated_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log "
                    "(principal, project, tool, object_kind, object_id, "
                    " transition, args_digest) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    ("alice", "proj", "allocations.grant", "allocations", uuid4(), "g", "d"),
                )
            assert await _count_audit(conn) == 1

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
