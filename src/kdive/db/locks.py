"""Transaction-scoped Postgres advisory locks (ADR-0005, ADR-0016).

`advisory_xact_lock` serializes per-Allocation / per-System operations using the
single-bigint `pg_advisory_xact_lock` — a lock space disjoint from the migration
runner's two-int lock (ADR-0015), so application and migration locks never contend.
The lock releases when the surrounding transaction ends; the helper fails fast when
no transaction is open to hold it.
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
    total order ``PROJECT → RESOURCE → ALLOCATION → SYSTEM`` (and then
    ``INVESTIGATION → RUN`` for run creation) to avoid deadlock; e.g.
    ``allocations.request`` takes ``PROJECT`` then ``RESOURCE`` (ADR-0040 §1), and
    ``runs.create`` takes ``SYSTEM`` then ``INVESTIGATION`` (ADR-0027).

    ``PROJECT`` is keyed by the ``project`` string; every other scope is keyed by an
    object :class:`~uuid.UUID`.
    """

    PROJECT = "project"
    ALLOCATION = "allocation"
    SYSTEM = "system"
    RESOURCE = "resource"
    INVESTIGATION = "investigation"
    RUN = "run"


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
