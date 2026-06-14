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
from kdive.config.core_settings import DATABASE_URL, SYSTEMS_TOML
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
    reconciled = _reconcile_inventory_images(url)
    print(f"reconciled {reconciled} inventory image(s) from systems.toml")
    seeded_configs = _seed_build_configs_step(url)
    print(f"seeded {seeded_configs} build-config fragment(s)")
    return len(applied)


def _seed_build_configs_step(database_url: str) -> int:
    """Publish the packaged build-config fragments after migrating (ADR-0096).

    Runs in the deploy ``migrate -> seed`` step. Idempotent (sha256-gated). The fragments
    live in the object store, so the seed is skipped when ``KDIVE_S3_*`` is unconfigured —
    a no-S3 migrate (e.g. a schema-only test or a partial bring-up) degrades cleanly and the
    fragment is seeded on a later migrate once the object store is available. Mirrors the
    images-tool tolerance in :func:`kdive.mcp.tools.ops.images.registrar._resolve_object_store`.

    Args:
        database_url: A psycopg-compatible connection string for the application database.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    import asyncio

    from kdive.build_configs.seed import seed_build_configs
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.store.objectstore import object_store_from_env

    try:
        store = object_store_from_env()
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.CONFIGURATION_ERROR:
            raise
        print("skipped build-config seed: object store not configured")
        return 0

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            return await seed_build_configs(conn, store)

    return asyncio.run(_run())


def _reconcile_inventory_images(database_url: str) -> int:
    """Reconcile ``systems.toml`` ``[[image]]`` entries into ``image_catalog`` (ADR-0112).

    Replaces the former packaged-YAML baseline seed: image definitions now live only in
    ``systems.toml`` (loaded into ``image_catalog`` by :func:`reconcile_images`), not in code.
    Runs as the deploy ``migrate → reconcile`` step so a fresh install lists the declared
    baseline before any image is built. Idempotent (change-detecting upserts).

    **Behavior change from the former seed (deploy note):** the old seed was additive-only
    (``ON CONFLICT DO NOTHING``); reconcile **prunes** any ``managed_by='config'`` row whose
    identity is absent from ``systems.toml``. On the first upgrade to this code, declare every
    baseline image you want kept in ``systems.toml`` before running ``migrate``, or its config
    row is pruned. Prune is row-delete-only and cordons (never deletes) an in-use image, and the
    S3 object is reclaimed by the existing leaked-image GC (ADR-0112) — so no image bytes or
    running systems are irreversibly destroyed by a deploy.

    The path is ``KDIVE_SYSTEMS_TOML`` (default ``./systems.toml``). An **absent** file is the
    normal pre-config state (the file is gitignored) and is a quiet no-op — feeding an empty
    document to ``reconcile_images`` would prune every config row, so the load uses
    :func:`load_inventory_optional` and short-circuits on ``None``. A present-but-malformed file
    raises :class:`~kdive.inventory.InventoryError` (a real operator error the deploy surfaces).

    The object store is used only to HEAD ``s3`` image objects for existence. When S3 is wholly
    unconfigured (e.g. a schema-only test or a partial bring-up), a no-op store is used so every
    ``s3`` image stays ``defined`` and the reconcile still succeeds — mirroring the no-S3
    tolerance of :func:`_seed_build_configs_step` and :func:`reconcile_images`'s own degradation.

    Args:
        database_url: A psycopg-compatible connection string for the application database.

    Returns:
        The number of catalog rows created/updated/pruned/cordoned (0 when no file is present).
    """
    import asyncio
    from pathlib import Path

    from kdive.inventory import load_inventory_optional
    from kdive.inventory.reconcile_images import reconcile_images

    raw_path = config.get(SYSTEMS_TOML) or "./systems.toml"
    doc = load_inventory_optional(Path(raw_path))
    if doc is None:
        print("skipped inventory reconcile: no systems.toml present")
        return 0
    store = _reconcile_image_store()

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            diff = await reconcile_images(conn, doc, store)
            return len(diff.created) + len(diff.updated) + len(diff.pruned) + len(diff.cordoned)

    return asyncio.run(_run())


def _reconcile_image_store() -> Any:
    """The object store for the migrate-time image reconcile, degrading when S3 is unconfigured.

    Returns the real :class:`~kdive.store.objectstore.ObjectStore` when ``KDIVE_S3_*`` is set,
    else a no-op store whose ``head_present`` always reports absent so every ``s3`` image stays
    ``defined`` (matching the former no-S3 seed behaviour). Only a configured-but-erroring store
    is a hard failure (``reconcile_images`` raises it).
    """
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.store.objectstore import object_store_from_env

    try:
        return object_store_from_env()
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.CONFIGURATION_ERROR:
            raise
        return _NoS3HeadStore()


class _NoS3HeadStore:
    """A store stub that reports every object absent (used when S3 is unconfigured)."""

    def head_present(self, key: str) -> bool:
        del key
        return False


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
