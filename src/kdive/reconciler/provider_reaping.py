"""Provider-owned infrastructure repair for the reconciler."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_STALE_AFTER
from kdive.domain.models import JobKind
from kdive.domain.state import JobState, SystemState
from kdive.providers.reaping import InfraReaper

_log = logging.getLogger(__name__)

_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES = (JobState.QUEUED.value, JobState.RUNNING.value)
_TEARDOWN_JOB_KIND_VALUE = JobKind.TEARDOWN.value
_TORN_DOWN_SYSTEM_STATE_VALUE = SystemState.TORN_DOWN.value


async def repair_leaked_domains(conn: AsyncConnection, reaper: InfraReaper) -> int:
    """Destroy provider domains whose tagged System is gone and no teardown is in flight."""
    domains = await reaper.list_owned()
    reaped = 0
    for domain in domains:
        if domain.system_id is None:
            continue
        async with (
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, domain.system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT 1 FROM systems WHERE id = %s AND state <> %s",
                (domain.system_id, _TORN_DOWN_SYSTEM_STATE_VALUE),
            )
            has_live_row = await cur.fetchone() is not None
            await cur.execute(
                "SELECT 1 FROM jobs WHERE state = ANY(%s) "
                "  AND kind = %s AND payload->>'system_id' = %s",
                (
                    list(_TEARDOWN_JOB_IN_FLIGHT_STATE_VALUES),
                    _TEARDOWN_JOB_KIND_VALUE,
                    str(domain.system_id),
                ),
            )
            teardown_in_flight = await cur.fetchone() is not None
        if has_live_row or teardown_in_flight:
            continue
        try:
            await reaper.destroy(domain.name)
        except Exception:  # noqa: BLE001 - one domain's failure must not strand the others
            _log.warning(
                "reconciler: destroy of leaked domain %s failed; retry next pass",
                domain.name,
                exc_info=True,
            )
            continue
        reaped += 1
        _log.info("reconciler: leaked domain %s (system %s) reaped", domain.name, domain.system_id)
    return reaped


class ProbeReaper(Protocol):
    """The narrow provider port the reconciler consumes to destroy a leaked probe guest.

    Structurally a subset of :class:`kdive.providers.reaping.InfraReaper` (``destroy(name)``),
    so the reconciler reuses its existing reaper for both the leaked-domain and the
    leaked-probe sweep — a probe guest is destroyed by domain name like any other domain.
    """

    async def destroy(self, name: str) -> None: ...


async def repair_leaked_probe_guests(
    conn: AsyncConnection,
    reaper: ProbeReaper,
    *,
    heartbeat_stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
) -> int:
    """Reap ``guest_egress`` probe guests whose owning doctor run is gone; honor the heartbeat.

    A probe is leaked when its marker row is past its hard TTL **or** its active-run heartbeat
    is stale (the owning ``doctor`` run stopped beating) — and is not already released. A row
    with a **fresh** heartbeat is an in-use probe (a live run) and is **never** reaped (ADR-0091
    §3): the reaper must not destroy a guest mid-check and turn a healthy egress path into a
    spurious ``error``. On a successful destroy the row is stamped ``released_at`` so the
    provider's single-flight slot frees and a re-pass does not re-reap. Per-probe ``destroy``
    failures are isolated (one leak must not strand the others); time predicates run in
    Postgres (never a Python clock).
    """
    rows = await _leaked_probe_rows(conn, heartbeat_stale_after)
    reaped = 0
    for row in rows:
        if not await _destroy_probe(reaper, row):
            continue
        await _mark_probe_released(conn, row["id"])
        reaped += 1
        _log.info("reconciler: leaked egress probe %s reaped", row["domain_name"])
    return reaped


async def _leaked_probe_rows(conn: AsyncConnection, stale_after: timedelta) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, domain_name FROM egress_probe_guests "
            "WHERE released_at IS NULL "
            "  AND (ttl_deadline < now() OR heartbeat_at < now() - %s)",
            (stale_after,),
        )
        return list(await cur.fetchall())


async def _destroy_probe(reaper: ProbeReaper, row: dict) -> bool:
    try:
        await reaper.destroy(row["domain_name"])
    except Exception:  # noqa: BLE001 - one probe's failure must not strand the others
        _log.warning(
            "reconciler: destroy of leaked egress probe %s failed; retry next pass",
            row["domain_name"],
            exc_info=True,
        )
        return False
    return True


async def _mark_probe_released(conn: AsyncConnection, probe_id: UUID) -> None:
    async with conn.transaction():
        await conn.execute(
            "UPDATE egress_probe_guests SET released_at = now() WHERE id = %s", (probe_id,)
        )
