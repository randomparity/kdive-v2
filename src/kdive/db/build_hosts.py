"""Async repository for build_hosts and build_host_leases (ADR-0099).

`get_by_name` resolves a host by its unique name. `lease_count` reads
how many in-flight leases a host currently holds. `try_acquire_lease`
serializes capacity admission under the BUILD_HOST advisory lock and is
idempotent for re-acquire (same run_id). `release_lease` deletes the
lease row and is safe to call when no lease exists.

All functions require an open psycopg AsyncConnection.  The caller is
responsible for wrapping `try_acquire_lease` in a transaction — the
BUILD_HOST advisory lock is transaction-scoped and must commit together
with whatever work is enqueued alongside it (e.g. a ``runs.build`` job).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.locks import LockScope, advisory_xact_lock

WORKER_LOCAL_ID = UUID("00000000-0000-0000-0000-0000000000c0")


@dataclass(slots=True, frozen=True)
class BuildHost:
    """A row from the ``build_hosts`` table.

    Attributes:
        id: Primary key.
        name: Unique human-readable identifier (e.g. ``worker-local``).
        kind: Transport kind — ``'local'`` or ``'ssh'``.
        address: SSH hostname or IP; ``None`` for local hosts.
        ssh_credential_ref: Credential secret reference; ``None`` for local hosts.
        workspace_root: Absolute path on the build host where builds are staged.
        max_concurrent: Maximum simultaneous build leases this host may hold.
        enabled: Whether the scheduler may select this host.
        state: Operational state — ``'ready'`` or ``'unreachable'``.
    """

    id: UUID
    name: str
    kind: str
    address: str | None
    ssh_credential_ref: str | None
    workspace_root: str
    max_concurrent: int
    enabled: bool
    state: str


async def get_by_name(conn: AsyncConnection, name: str) -> BuildHost | None:
    """Return the build host with ``name``, or ``None`` if not found.

    Args:
        conn: An async psycopg connection (autocommit or inside a transaction).
        name: The unique host name to look up.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM build_hosts WHERE name = %s", (name,))
        row = await cur.fetchone()
    if row is None:
        return None
    return BuildHost(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        address=row["address"],
        ssh_credential_ref=row["ssh_credential_ref"],
        workspace_root=row["workspace_root"],
        max_concurrent=row["max_concurrent"],
        enabled=row["enabled"],
        state=row["state"],
    )


async def lease_count(conn: AsyncConnection, host_id: UUID) -> int:
    """Return how many active leases ``host_id`` currently holds.

    Args:
        conn: An async psycopg connection.
        host_id: The build host's primary key.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM build_host_leases WHERE build_host_id = %s",
            (host_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def try_acquire_lease(conn: AsyncConnection, host: BuildHost, run_id: UUID) -> bool:
    """Acquire one capacity lease for ``run_id`` under the BUILD_HOST advisory lock.

    The caller must be inside an open transaction so the lease INSERT and the
    lock both commit atomically with the surrounding work (e.g. a build-job
    enqueue). Idempotent: a re-acquire for the same ``run_id`` returns ``True``
    without inserting a second row.

    Args:
        conn: An async psycopg connection with an open transaction.
        host: The build host to acquire capacity on.
        run_id: The run that is consuming the lease.

    Returns:
        ``True`` if a lease exists (or was just created) for ``run_id``.
        ``False`` if the host is already at full capacity and no existing
        lease for ``run_id`` was found.
    """
    async with advisory_xact_lock(conn, LockScope.BUILD_HOST, host.id):
        # Idempotent: if this run already has a lease, return True immediately.
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM build_host_leases WHERE run_id = %s",
                (run_id,),
            )
            existing = await cur.fetchone()
        if existing is not None:
            return True

        # Capacity check under the lock.
        count = await lease_count(conn, host.id)
        if count >= host.max_concurrent:
            return False

        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
                (run_id, host.id),
            )
        return True


async def release_lease(conn: AsyncConnection, run_id: UUID) -> None:
    """Delete the lease for ``run_id`` if one exists; a no-op when already absent.

    Args:
        conn: An async psycopg connection (autocommit or inside a transaction).
        run_id: The run whose lease should be dropped.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM build_host_leases WHERE run_id = %s",
            (run_id,),
        )
