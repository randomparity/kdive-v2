"""Installed-package admin helpers: migrate, install-fixtures, seed-demo.

The app-process bring-up (the `stack` supervisor and the `install-compose`/
`print-local-env` dev crutches) was retired in ADR-0088 decision 9: the published
image — or the compose app tier — is the bring-up path. Only the real operations the
image still invokes remain here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES
from kdive.config.core_settings import DATABASE_URL
from kdive.db.migrate import apply_migrations


def default_fixture_files() -> Mapping[str, str]:
    return LOCAL_LIBVIRT_FIXTURES


def _refuse_existing(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")


def install_fixtures(dest: Path, *, force: bool = False) -> None:
    _refuse_existing(dest, force=force)
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def migrate(database_url: str | None = None) -> int:
    url = database_url or config.require(DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(f"applied {len(applied)} migration(s)")
    seeded = _seed_baseline_rootfs(url)
    print(f"seeded {seeded} baseline rootfs image(s)")
    return len(applied)


def _seed_baseline_rootfs(database_url: str) -> int:
    """Register the baseline rootfs as `defined` catalog rows after migrating (ADR-0092).

    Runs as the deploy ``migrate → seed`` step so a fresh install lists the baseline before any
    image is built. Idempotent and read-only against operator data.
    """
    import asyncio

    from kdive.images.seed import seed_defined_rootfs

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            return await seed_defined_rootfs(conn)

    return asyncio.run(_run())


def seed_project_statements(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> list[tuple[str, Sequence[Any]]]:
    return [
        (
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
            "VALUES (%s, %s, 0) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        ),
        (
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_concurrent_allocations, max_concurrent_systems),
        ),
    ]


async def seed_demo(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> None:
    """Seed budget/quota rows and register the local provider resource."""
    from kdive.db.pool import create_pool

    pool = create_pool()
    await pool.open()
    try:
        async with pool.connection() as conn, conn.transaction():
            for statement, params in seed_project_statements(
                project=project,
                limit_kcu=limit_kcu,
                max_concurrent_allocations=max_concurrent_allocations,
                max_concurrent_systems=max_concurrent_systems,
            ):
                await conn.execute(statement.encode(), params)
        await register_local_resource(pool)
    finally:
        await pool.close()


async def register_local_resource(pool: AsyncConnectionPool) -> None:
    from kdive.providers.composition import build_provider_resolver

    await build_provider_resolver().register_all_discovery(pool)
