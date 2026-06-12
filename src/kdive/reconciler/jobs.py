"""Job-row repair for the reconciler."""

from __future__ import annotations

import logging

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.errors import ErrorCategory
from kdive.domain.models import JobKind
from kdive.domain.state import JobState, RunState
from kdive.jobs.payloads import PayloadValidationError, run_id_from_payload

_log = logging.getLogger(__name__)

_RUN_COMPENSATION_STATES = (RunState.CREATED, RunState.RUNNING)
_RUN_COMPENSATION_STATE_VALUES = tuple(state.value for state in _RUN_COMPENSATION_STATES)
_FAILED_JOB_STATE_VALUE = JobState.FAILED.value
_RUNNING_JOB_STATE_VALUE = JobState.RUNNING.value
_FAILED_RUN_STATE_VALUE = RunState.FAILED.value
_LEASE_EXPIRED_CATEGORY_VALUE = ErrorCategory.LEASE_EXPIRED.value


async def repair_abandoned_jobs(conn: AsyncConnection) -> int:
    """Dead-letter zombie jobs the worker can never reclaim, compensating their Run."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id FROM jobs "
            "WHERE state = %s AND lease_expires_at < now() "
            "  AND attempt >= max_attempts",
            (_RUNNING_JOB_STATE_VALUE,),
        )
        zombie_ids = [row["id"] for row in await cur.fetchall()]
    swept = 0
    for job_id in zombie_ids:
        async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE jobs SET state = %s, error_category = %s "
                "WHERE id = %s AND state = %s RETURNING kind, payload",
                (
                    _FAILED_JOB_STATE_VALUE,
                    _LEASE_EXPIRED_CATEGORY_VALUE,
                    job_id,
                    _RUNNING_JOB_STATE_VALUE,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                continue
            try:
                run_id = run_id_from_payload(JobKind(row["kind"]), row["payload"])
            except PayloadValidationError as exc:
                _log.warning(
                    "reconciler: abandoned job %s has invalid payload; "
                    "skipping Run compensation: %s",
                    job_id,
                    exc,
                )
                run_id = None
            if run_id is not None:
                await cur.execute(
                    "UPDATE runs SET state = %s, failure_category = %s "
                    "WHERE id = %s AND state = ANY(%s)",
                    (
                        _FAILED_RUN_STATE_VALUE,
                        _LEASE_EXPIRED_CATEGORY_VALUE,
                        run_id,
                        list(_RUN_COMPENSATION_STATE_VALUES),
                    ),
                )
        swept += 1
        _log.info("reconciler: abandoned job %s -> failed (lease_expired)", job_id)
    return swept
