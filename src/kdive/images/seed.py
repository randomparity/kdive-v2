"""App-level baseline-rootfs seed (ADR-0092).

The SQL migration only creates the table; ``db/migrate.py`` applies ``NNNN_*.sql`` and cannot
read fixture YAML. This step registers the baseline rootfs catalog as ``defined`` rows
(metadata, ``object_key`` NULL) so a fresh install lists the baseline before any image is built;
``images build``/``publish`` later realizes a ``defined`` row to ``registered``. The seed reads
the operator-configured catalog (``FIXTURE_CATALOG_PATH``) or, by default, the packaged baseline
relocated into ``images/seed_data/``. It is **read-only against operator data** — it never
deletes or rewrites the files it read — and idempotent (an identity already present is skipped).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.provider_components.catalog import (
    RootfsCatalogEntry,
    fixture_catalog_path_from_env,
    load_fixture_catalog,
)

PACKAGED_SEED_DATA_PATH = Path(__file__).parent / "seed_data"


def _seed_path(path: Path | None) -> Path:
    """Resolve the catalog directory: explicit arg, then ``FIXTURE_CATALOG_PATH``, else packaged.

    The operator override path is honored when set so a customized catalog is not silently
    dropped; otherwise the packaged ``seed_data/`` baseline ships with this package.
    """
    if path is not None:
        return path
    from_env = fixture_catalog_path_from_env()
    # `fixture_catalog_path_from_env` returns the source-tree fixtures default when unset; that
    # tree no longer holds rootfs entries, so fall back to the packaged baseline instead.
    if not _env_override_set():
        return PACKAGED_SEED_DATA_PATH
    return from_env


def _env_override_set() -> bool:
    import kdive.config as config
    from kdive.config.core_settings import FIXTURE_CATALOG_PATH

    raw = config.get(FIXTURE_CATALOG_PATH)
    return bool(raw)


def _defined_row(entry: RootfsCatalogEntry) -> ImageCatalogEntry:
    """Build a ``defined`` image_catalog row from a baseline rootfs catalog entry.

    Only public baseline entries seed (a private image is never a baseline); ``object_key`` and
    ``digest`` are NULL because the image bytes are not built yet.
    """
    now = datetime.now(UTC)
    return ImageCatalogEntry(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        pending_since=now,
        provider=entry.provider,
        name=entry.name,
        arch=entry.arch,
        format=entry.format,
        root_device=entry.root_device,
        object_key=None,
        digest=None,
        capabilities=list(entry.capabilities),
        provenance={},
        visibility=ImageVisibility.PUBLIC,
        owner=None,
        expires_at=None,
        state=ImageState.DEFINED,
    )


async def _identity_exists(conn: AsyncConnection, entry: RootfsCatalogEntry) -> bool:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE provider = %(provider)s AND name = %(name)s AND arch = %(arch)s "
            "AND visibility = %(public)s LIMIT 1",
            {
                "provider": entry.provider,
                "name": entry.name,
                "arch": entry.arch,
                "public": ImageVisibility.PUBLIC.value,
            },
        )
        return await cur.fetchone() is not None


async def seed_defined_rootfs(conn: AsyncConnection, path: Path | None = None) -> int:
    """Register the baseline rootfs catalog as ``defined`` rows, idempotently.

    Args:
        conn: An async Postgres connection.
        path: Catalog directory to read. Defaults to ``FIXTURE_CATALOG_PATH`` when the operator
            set one, else the packaged baseline (``images/seed_data/``).

    Returns:
        The count of identities newly registered (already-present identities are skipped).
    """
    catalog = load_fixture_catalog(_seed_path(path))
    registered = 0
    for entry in catalog.rootfs:
        if entry.visibility != "public":
            continue
        if await _identity_exists(conn, entry):
            continue
        await IMAGE_CATALOG.insert(conn, _defined_row(entry))
        registered += 1
    return registered
