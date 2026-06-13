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
from typing import cast
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
        kind: Transport kind — ``'local'``, ``'ssh'``, or ``'ephemeral_libvirt'``.
        address: SSH hostname or IP; ``None`` for local and ephemeral-libvirt hosts.
        ssh_credential_ref: Credential secret reference; ``None`` for local and
            ephemeral-libvirt hosts.
        base_image_volume: Operator-staged base build-image volume name; set only for
            ``ephemeral_libvirt`` hosts (``None`` otherwise).
        workspace_root: Absolute path where builds are staged (the in-guest path for
            ``ephemeral_libvirt``).
        max_concurrent: Maximum simultaneous build leases this host may hold.
        enabled: Whether the scheduler may select this host.
        state: Operational state — ``'ready'`` or ``'unreachable'``.
    """

    id: UUID
    name: str
    kind: str
    address: str | None
    ssh_credential_ref: str | None
    base_image_volume: str | None
    workspace_root: str
    max_concurrent: int
    enabled: bool
    state: str


def _row_to_host(row: dict[str, object]) -> BuildHost:
    return BuildHost(
        id=cast(UUID, row["id"]),
        name=cast(str, row["name"]),
        kind=cast(str, row["kind"]),
        address=cast("str | None", row["address"]),
        ssh_credential_ref=cast("str | None", row["ssh_credential_ref"]),
        base_image_volume=cast("str | None", row["base_image_volume"]),
        workspace_root=cast(str, row["workspace_root"]),
        max_concurrent=cast(int, row["max_concurrent"]),
        enabled=cast(bool, row["enabled"]),
        state=cast(str, row["state"]),
    )


async def get_by_name(conn: AsyncConnection, name: str) -> BuildHost | None:
    """Return the build host with ``name``, or ``None`` if not found.

    Args:
        conn: An async psycopg connection (autocommit or inside a transaction).
        name: The unique host name to look up.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM build_hosts WHERE name = %s", (name,))
        row = await cur.fetchone()
    return None if row is None else _row_to_host(row)


async def get_by_id(conn: AsyncConnection, host_id: UUID) -> BuildHost | None:
    """Return the build host with ``host_id``, or ``None`` if not found.

    The build job payload carries the selected host's id (it was admitted under capacity at
    the ``runs.build`` boundary), so the worker resolves the host by id rather than name.

    Args:
        conn: An async psycopg connection (autocommit or inside a transaction).
        host_id: The build host's primary key.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM build_hosts WHERE id = %s", (host_id,))
        row = await cur.fetchone()
    return None if row is None else _row_to_host(row)


async def list_probeable_ssh_hosts(conn: AsyncConnection) -> list[BuildHost]:
    """Return the SSH build hosts a reachability probe should check (ADR-0103).

    The probe set is ``kind='ssh' AND enabled=true``: a ``local`` host has no address to
    probe, and a disabled host is never selected, so probing it would spend an SSH
    connection for no behavioral effect (it is re-probed the pass after it is re-enabled).

    Args:
        conn: An async psycopg connection (autocommit or inside a transaction).

    Returns:
        The matching :class:`BuildHost` rows, ordered by name.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM build_hosts WHERE kind = 'ssh' AND enabled = true ORDER BY name"
        )
        rows = await cur.fetchall()
    return [_row_to_host(row) for row in rows]


async def mark_state(
    conn: AsyncConnection, host_id: UUID, *, new_state: str, expected_state: str
) -> int:
    """Compare-and-swap a build host's ``state`` (ADR-0103).

    Writes ``new_state`` only when the row's ``state`` still equals ``expected_state`` (the
    value the caller observed), so a concurrent operator change is never clobbered and a
    no-op probe (state unchanged) writes nothing. The ``set_updated_at`` trigger fires only
    on an actual value change, so ``updated_at`` advances only on a real transition.

    Args:
        conn: An async psycopg connection. The caller wraps the call in a committed
            transaction so the flip is durable.
        host_id: The build host's primary key.
        new_state: The state to write (``'ready'`` or ``'unreachable'``).
        expected_state: The state the caller observed; the write is skipped if it no longer
            holds.

    Returns:
        The number of rows updated: ``1`` on a successful flip, ``0`` when the observed
        state no longer holds (or the host is gone).
    """
    cur = await conn.execute(
        "UPDATE build_hosts SET state = %s WHERE id = %s AND state = %s",
        (new_state, host_id, expected_state),
    )
    return cur.rowcount


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
    enqueue).

    Idempotency is **host-scoped**: a re-acquire for the same ``run_id`` against
    the same ``host`` returns ``True`` without inserting a second row. A call for
    a ``run_id`` that already holds a lease against a *different* host does not
    match this host's existing-lease check, so it falls through to the capacity
    check and INSERT, which raises ``psycopg.errors.UniqueViolation`` on the
    ``run_id`` primary key — a run may hold at most one lease, on one host.

    Args:
        conn: An async psycopg connection with an open transaction.
        host: The build host to acquire capacity on.
        run_id: The run that is consuming the lease.

    Returns:
        ``True`` if a lease exists (or was just created) for ``run_id`` on this
        host. ``False`` if the host is already at full capacity and no existing
        lease for ``run_id`` against this host was found.

    Raises:
        psycopg.errors.UniqueViolation: ``run_id`` already holds a lease against
            a different host.
    """
    async with advisory_xact_lock(conn, LockScope.BUILD_HOST, host.id):
        # Host-scoped idempotency: a lease for this run AGAINST THIS HOST is a
        # no-op success. A lease against a different host does not match here and
        # falls through to the INSERT, which surfaces the run_id PK conflict.
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM build_host_leases WHERE run_id = %s AND build_host_id = %s",
                (run_id, host.id),
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

    Rollback hazard: this DELETE is bound to ``conn``'s transaction. If the
    surrounding transaction later rolls back, the DELETE rolls back with it and
    the lease survives — the slot stays occupied until the reconciler reclaims
    it. The build handler must therefore release on a path that commits (an
    autocommit connection, or a dedicated transaction committed before any
    failure path) so a build failure reliably frees the slot.

    Args:
        conn: An async psycopg connection. Use autocommit or a committed
            transaction so the release durably frees the slot (see hazard above).
        run_id: The run whose lease should be dropped.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM build_host_leases WHERE run_id = %s",
            (run_id,),
        )
