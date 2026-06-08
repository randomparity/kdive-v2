"""jobs.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.models import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import BuildPayload
from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.catalog import jobs as jobs_tools
from kdive.security.authz.rbac import AuthorizationError, Role

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
OP_CTX = RequestContext(
    principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.OPERATOR}
)
VIEWER_CTX = RequestContext(
    principal="user-1", agent_session="s", projects=("proj",), roles={"proj": Role.VIEWER}
)


def _build_payload() -> BuildPayload:
    return BuildPayload(run_id=str(uuid4()))


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _enqueue_in(pool: AsyncConnectionPool, dedup: str, project: str) -> str:
    """Enqueue a job whose authorizing tuple is owned by ``project``."""
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn, JobKind.BUILD, _build_payload(), {"principal": "p", "project": project}, dedup
        )
    return str(job.id)


async def _enqueue(pool: AsyncConnectionPool, dedup: str) -> str:
    """Enqueue a job in ``CTX``'s project (the common case for these tests)."""
    return await _enqueue_in(pool, dedup, "proj")


def test_get_known_job_returns_status(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert resp.object_id == job_id
        assert resp.status == "queued"
        assert resp.data == {"kind": "build"}

    asyncio.run(_run())


def test_get_unknown_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_malformed_id_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.get_job(pool, CTX, "not-a-uuid")
        assert resp.object_id == "not-a-uuid"
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cancel_queued_job_transitions(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_cancel_terminal_job_is_error_envelope(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # -> canceled (terminal)
            resp = await jobs_tools.cancel_job(pool, OP_CTX, job_id)  # again
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        # The agent learns the current state without a second jobs.get.
        assert resp.data == {"current_status": "canceled"}

    asyncio.run(_run())


def test_wait_returns_immediately_for_terminal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            await jobs_tools.cancel_job(pool, OP_CTX, job_id)
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=5.0)
        assert resp.status == "canceled"

    asyncio.run(_run())


def test_wait_zero_timeout_is_single_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=0.0)
        assert resp.status == "queued"  # one read, no wait

    asyncio.run(_run())


def test_wait_caps_sleep_to_remaining_timeout(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")  # stays queued (no worker)
            sleeps: list[float] = []

            async def _sleep(delay: float) -> None:
                sleeps.append(delay)
                assert delay <= 0.05
                await asyncio.sleep(delay)

            resp = await jobs_tools.wait_job(
                pool,
                VIEWER_CTX,
                job_id,
                timeout_s=0.05,
                sleep=_sleep,
            )
        assert resp.status == "queued"
        assert len(sleeps) == 1

    asyncio.run(_run())


@pytest.mark.parametrize("timeout_s", [float("nan"), float("inf"), float("-inf")])
def test_wait_non_finite_timeout_is_configuration_error(
    migrated_url: str, timeout_s: float
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            resp = await jobs_tools.wait_job(pool, VIEWER_CTX, job_id, timeout_s=timeout_s)
        assert resp.object_id == job_id
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_wait_loops_until_terminal(migrated_url: str) -> None:
    """Exercise the sleep-then-re-poll branch without a wall-clock delay."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            polls = 0

            async def _cancel_after_first_poll(_: float) -> None:
                nonlocal polls
                polls += 1
                await jobs_tools.cancel_job(pool, OP_CTX, job_id)

            resp = await jobs_tools.wait_job(
                pool,
                VIEWER_CTX,
                job_id,
                timeout_s=5.0,
                sleep=_cancel_after_first_poll,
            )
        assert resp.status == "canceled"
        assert polls == 1

    asyncio.run(_run())


def test_list_jobs_newest_first_and_capped(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _enqueue(pool, f"d{i}")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=2)
        items = resp.items
        assert resp.object_id == "jobs"
        assert resp.status == "ok"
        assert resp.data["count"] == "2"
        assert len(items) == 2
        assert all(r.status == "queued" for r in items)

    asyncio.run(_run())


def test_list_jobs_empty(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        assert resp.status == "ok"
        assert resp.items == []

    asyncio.run(_run())


def test_list_jobs_isolates_invariant_violating_row(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A single producer-bug row (failed with no category) degrades to an error
    envelope without blanking the rest of the list."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            good_id = await _enqueue(pool, "good")
            bad_id = await _enqueue(pool, "bad")
            # Force the bad row into a state that violates "category iff failed".
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "UPDATE jobs SET state = 'failed', error_category = NULL WHERE id = %s",
                    (bad_id,),
                )
            caplog.set_level(logging.WARNING, logger=jobs_tools.__name__)
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        items = resp.items
        by_id = {r.object_id: r for r in items}
        assert len(items) == 2  # the bad row did not blank the list
        assert by_id[good_id].status == "queued"
        assert by_id[bad_id].status == "error"
        assert by_id[bad_id].error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"job {bad_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


# --- cross-project isolation (#11): a job is visible only to its project's members ---

_OTHER = RequestContext(principal="user-2", agent_session="s", projects=("other",))


def test_get_job_in_unowned_project_is_indistinguishable_from_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            # _OTHER is a member of "other", not "proj": the job must look absent (no leak).
            resp = await jobs_tools.get_job(pool, _OTHER, job_id)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.object_id == job_id

    asyncio.run(_run())


def test_wait_job_in_unowned_project_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            resp = await jobs_tools.wait_job(pool, _OTHER, job_id, timeout_s=0.0)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_get_job_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(AuthorizationError):
                await jobs_tools.get_job(pool, CTX, job_id)

    asyncio.run(_run())


def test_wait_job_requires_viewer_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(AuthorizationError):
                await jobs_tools.wait_job(pool, CTX, job_id, timeout_s=0.0)

    asyncio.run(_run())


def test_cancel_job_in_unowned_project_is_denied_and_does_not_mutate(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue_in(pool, "d1", "proj")
            denied = await jobs_tools.cancel_job(pool, _OTHER, job_id)
            # The owning project's member still sees it queued — the cancel did not land.
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert denied.status == "error"
        assert denied.error_category == "configuration_error"
        assert owned.status == "queued"

    asyncio.run(_run())


def test_cancel_job_requires_operator_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(AuthorizationError):
                await jobs_tools.cancel_job(pool, VIEWER_CTX, job_id)
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert owned.status == "queued"

    asyncio.run(_run())


def test_cancel_job_requires_a_project_role(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            job_id = await _enqueue(pool, "d1")
            with pytest.raises(AuthorizationError):
                await jobs_tools.cancel_job(pool, CTX, job_id)
            owned = await jobs_tools.get_job(pool, VIEWER_CTX, job_id)
        assert owned.status == "queued"

    asyncio.run(_run())


def test_list_jobs_only_returns_callers_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            mine = await _enqueue_in(pool, "mine", "proj")
            await _enqueue_in(pool, "theirs", "other")
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        ids = {r.object_id for r in resp.items}
        assert ids == {mine}  # the "other"-project job is not listed

    asyncio.run(_run())


def test_list_jobs_excludes_roleless_projects(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _enqueue(pool, "d1")
            resp = await jobs_tools.list_jobs(pool, CTX, limit=50)
        assert resp.items == []

    asyncio.run(_run())


def test_list_jobs_excludes_jobs_with_no_project(migrated_url: str) -> None:
    # A job whose authorizing tuple carries no project belongs to no one: fail closed.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
                    "VALUES ('build', %s, 'queued', 3, %s, 'noproj')",
                    (
                        Jsonb(_build_payload().model_dump(mode="json", exclude_none=True)),
                        Jsonb({"principal": "p"}),
                    ),
                )
            resp = await jobs_tools.list_jobs(pool, VIEWER_CTX, limit=50)
        assert resp.items == []

    asyncio.run(_run())
