"""Postgres advisory locks (ADR-0005, ADR-0016, ADR-0095).

`advisory_xact_lock` serializes per-Allocation / per-System operations using the
single-bigint `pg_advisory_xact_lock` — a lock space disjoint from the migration
runner's two-int lock (ADR-0015), so application and migration locks never contend.
The lock releases when the surrounding transaction ends; the helper fails fast when
no transaction is open to hold it.

`SessionAdvisoryLock` is the **session-scoped** leadership claim the reconciler's
console-collector hosting loop needs (ADR-0095): a `pg_advisory_lock` held on a
dedicated long-lived connection that survives transaction boundaries and is released
**by Postgres the instant the holding connection drops** — the property the
single-leader split-brain guard relies on. Its key space is salted apart from the
transaction-scoped scope-lock space so leadership never collides with a per-object op.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus


class LockScope(StrEnum):
    """The advisory-lock scopes the platform serializes on (ADR-0016, ADR-0040).

    Operations that hold more than one scope at once acquire them in the fixed global
    total order ``PROJECT → RESOURCE → ALLOCATION → SYSTEM → INVESTIGATION → RUN →
    BUILD_HOST`` to avoid deadlock; e.g. ``allocations.request`` takes ``PROJECT`` then
    ``RESOURCE`` (ADR-0040 §1), ``runs.create`` takes ``SYSTEM`` then ``INVESTIGATION``
    (ADR-0027), and ``runs.build`` acquires ``BUILD_HOST`` inside an existing ``RUN``
    transaction — so every co-hold takes ``RUN`` before ``BUILD_HOST``.

    ``PROJECT`` is keyed by the ``project`` string; every other scope is keyed by an
    object :class:`~uuid.UUID`.
    """

    PROJECT = "project"
    ALLOCATION = "allocation"
    SYSTEM = "system"
    RESOURCE = "resource"
    INVESTIGATION = "investigation"
    RUN = "run"
    BUILD_HOST = "build_host"
    INVENTORY = "inventory"


def _lock_key(scope: LockScope, key: UUID | str) -> int:
    """Derive a deterministic signed 64-bit advisory-lock key from ``(scope, key)``.

    The digest folds an unbounded key space onto 64 bits: a collision over-serializes
    two unrelated keys (safe — never under-serializes). A ``0x00`` separator keeps the
    scope and key boundaries unambiguous for the NUL-free identifiers used here
    (object UUIDs and ``project`` strings).
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(scope.value.encode())
    digest.update(b"\x00")
    digest.update(str(key).encode())
    return int.from_bytes(digest.digest(), "big", signed=True)


@asynccontextmanager
async def advisory_xact_lock(
    conn: AsyncConnection, scope: LockScope, key: UUID | str
) -> AsyncIterator[None]:
    """Hold a transaction-scoped advisory lock for ``(scope, key)`` over the block.

    Blocks until any current holder's transaction ends, then yields. The lock is
    released by the caller's transaction commit/rollback, not on block exit.

    Args:
        conn: An async connection with an open (or about-to-open) transaction.
        scope: The lock scope.
        key: The object id (a :class:`~uuid.UUID`) the lock protects, or the
            ``project`` string for :attr:`LockScope.PROJECT`.

    Raises:
        RuntimeError: After acquiring, the connection is not in a transaction, so the
            lock already auto-released (e.g. an autocommit connection used without
            ``conn.transaction()``).
    """
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_lock_key(scope, key),))
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "advisory_xact_lock must run inside an open transaction; the lock "
            "auto-released because no transaction is in progress (ADR-0005). Wrap the "
            "call in `async with conn.transaction()` or use a non-autocommit connection."
        )
    yield


# The leadership name for the single reconciler that hosts console collectors (ADR-0095).
CONSOLE_HOSTING_LEADER = "console-hosting-leader"

# The single global inventory-reconcile pass name (ADR-0112). A whole reconcile pass spans
# multiple transactions (the batched upsert + per-row prunes), so it serializes on a
# session-scoped lock (released by Postgres if the holding connection drops), not an xact lock.
INVENTORY_RECONCILE = "inventory-reconcile"

# Salt that keeps the session-lock key space disjoint from the transaction-scoped
# scope-lock space: both fold onto the single-bigint advisory space, but a leadership
# claim must never serialize against a per-object op that happened to hash equal.
_SESSION_LOCK_SALT = b"session-advisory-lock\x00"


def _session_lock_key(name: str) -> int:
    """Derive a deterministic signed 64-bit session advisory-lock key from ``name``.

    Salted apart from :func:`_lock_key` so a session leadership claim and a
    transaction-scoped scope lock can never collide in the shared advisory space.
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(_SESSION_LOCK_SALT)
    digest.update(name.encode())
    return int.from_bytes(digest.digest(), "big", signed=True)


@asynccontextmanager
async def session_advisory_lock(conn: AsyncConnection, name: str) -> AsyncIterator[None]:
    """Hold a **session-scoped** advisory lock named ``name`` for the whole ``with`` block.

    Unlike :func:`advisory_xact_lock`, this lock survives transaction boundaries: it is
    acquired with ``pg_advisory_lock`` and released with ``pg_advisory_unlock`` in a
    ``finally``, so a multi-transaction operation (e.g. an inventory reconcile pass whose
    upsert and per-row prunes are separate transactions) serializes for its entire duration.
    The key space is salted apart from the transaction-scoped scope-lock space, so it never
    collides with a per-object op.

    The lock blocks until any current holder releases, then yields. The connection must not
    be in autocommit-with-implicit-transaction surprise: ``pg_advisory_lock`` taken outside a
    transaction is held at session scope, which is exactly the intent here.

    Args:
        conn: The connection that runs the whole guarded pass.
        name: The lock name (e.g. :data:`INVENTORY_RECONCILE`).
    """
    key = _session_lock_key(name)
    await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


class SessionAdvisoryLock:
    """A session-scoped ``pg_advisory_lock`` leadership claim on one dedicated connection.

    Unlike :func:`advisory_xact_lock`, this lock is **not** released at transaction end —
    it lives until :meth:`release` is called or the holding connection drops (which
    Postgres detects, releasing the lock with no notice to the dead holder). That is
    precisely the leadership semantics the console-hosting loop needs (ADR-0095), and the
    split-brain hazard the hosting loop's lock-loss guard handles: a standby can acquire
    the lock the instant the old leader's connection dies.

    The connection must be dedicated to leadership (outside the repair pool, ADR-0095):
    holding a pooled connection for the process life would pin the pool's only connection
    and starve the reconciler's repairs.
    """

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self._conn = conn
        self._key = _session_lock_key(name)
        # Postgres splits a single-bigint advisory lock into the two oid columns
        # pg_locks exposes: classid = high 32 bits, objid = low 32 bits (both unsigned).
        # Deriving them here avoids signed-int4 bit-math in SQL for negative keys.
        unsigned = self._key & 0xFFFF_FFFF_FFFF_FFFF
        self._classid = (unsigned >> 32) & 0xFFFF_FFFF
        self._objid = unsigned & 0xFFFF_FFFF

    async def try_acquire(self) -> bool:
        """Try to claim leadership without blocking; ``True`` iff this call won it.

        ``pg_try_advisory_lock`` never waits — a non-leader replica gets ``False``
        immediately and hosts nothing, rather than blocking behind the live leader.
        """
        async with self._conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (self._key,))
            row = await cur.fetchone()
        return bool(row[0]) if row is not None else False

    async def release(self) -> None:
        """Release the leadership lock if this connection holds it (idempotent)."""
        await self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))

    async def is_held(self) -> bool:
        """Report whether **this** connection currently holds the leadership lock.

        Reads ``pg_locks`` for this connection's backend pid, so it observes a release
        caused by a dropped connection that the dead holder could never report itself.
        """
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                "  AND classid = %s AND objid = %s AND objsubid = 1",
                (self._classid, self._objid),
            )
            row = await cur.fetchone()
        return bool(row[0]) if row is not None else False
