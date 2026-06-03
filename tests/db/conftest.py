"""Disposable-Postgres fixtures for the db tests (ADR-0015).

`postgres_url` starts one container per session and yields a psycopg-style
conninfo. When the Docker daemon is unreachable the fixture skips, unless
`KDIVE_REQUIRE_DOCKER=1` (set in CI), which turns the skip into a hard failure so
a broken runner cannot mask the suite. `pg_conn` gives each test a connection to a
freshly-emptied `public` schema.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

_POSTGRES_IMAGE = "postgres:17"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    container = PostgresContainer(_POSTGRES_IMAGE)
    try:
        container.start()
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
    try:
        yield container.get_connection_url(driver=None)
    finally:
        container.stop()


@pytest.fixture
def pg_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        yield conn
