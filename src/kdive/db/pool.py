"""Async Postgres connection pool from the environment (ADR-0005).

The runtime core, worker, and reconciler share a :class:`AsyncConnectionPool`
built from ``KDIVE_DATABASE_URL``. Schema creation is the synchronous runner in
:mod:`kdive.db.migrate`, not this module.
"""

from __future__ import annotations

import os

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory

_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"


def database_url() -> str:
    """Return the Postgres conninfo from the environment, failing fast if unset.

    Raises:
        CategorizedError: ``KDIVE_DATABASE_URL`` is unset
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    url = os.environ.get(_DATABASE_URL_ENV)
    if not url:
        raise CategorizedError(
            f"{_DATABASE_URL_ENV} is not set; cannot connect to Postgres",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return url


def create_pool(
    conninfo: str | None = None,
    *,
    min_size: int = 1,
    max_size: int = 10,
) -> AsyncConnectionPool:
    """Build an unopened async connection pool.

    Args:
        conninfo: Postgres conninfo; read from :func:`database_url` when omitted.
        min_size: Minimum pooled connections kept open.
        max_size: Maximum concurrent connections. The worker needs ``>= 2`` (a
            dispatched job holds its handler connection and a heartbeat connection at
            once); :class:`kdive.jobs.worker.Worker` rejects a smaller pool.

    Returns:
        A pool that is not yet open — enter it (``async with``) or call
        ``await pool.open()`` before use.
    """
    return AsyncConnectionPool(
        conninfo or database_url(), min_size=min_size, max_size=max_size, open=False
    )
