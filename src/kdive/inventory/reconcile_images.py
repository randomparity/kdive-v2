"""The image merge-reconcile (M2.6 #390, ADR-0112).

:func:`reconcile_images` is the load-bearing contract: it upserts the ``systems.toml``
``[[image]]`` entries into ``image_catalog`` keyed by ``(provider, name, arch)``, realizes
each per its source kind, and prunes config rows that left the file — all under one
session-scoped pass lock so concurrent passes never race the identity constraint.

Invariants (plan Task 1.4):

* **Never** writes runtime-owned ``object_key``/``digest``/``state`` of a build-realized row
  (a ``build`` source never downgrades a ``registered`` row to ``defined``).
* Identity match is scoped to ``managed_by='config'`` rows only — ``(provider, name, arch)``
  is not uniquely constrained, so a project-private upload can share it and must be left
  untouched.
* Change-detecting upserts: a row is appended to ``updated`` (and written) only when a
  config-owned field actually differs, so a steady state is a clean no-op (idempotent) and
  the loop never reports phantom drift.
* ``s3`` realization HEADs the object and degrades on **both** a 404 and a store-unreachable
  error — the row stays ``defined`` + warns, the pass still succeeds.
* Prune is **row-delete-only**: a referenced base image is cordoned (kind-aware guard), an
  idle one's row is deleted; the S3 object is reclaimed by the existing
  ``repair_leaked_images`` sweep, never by an inline ``store.delete`` (ADR-0112).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum, auto
from typing import Protocol, runtime_checkable
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError
from kdive.domain.models import ImageState, ManagedBy
from kdive.inventory.model import (
    BuildSource,
    ImageEntry,
    InventoryDoc,
    S3Source,
    StagedSource,
)
from kdive.inventory.reconcile import (
    ReconcileDiff,
    ReconcileRecord,
    inventory_pass_lock,
    prune_or_cordon_image,
)

_log = logging.getLogger(__name__)

_CONFIG = ManagedBy.CONFIG.value
_DEFINED = ImageState.DEFINED.value
_REGISTERED = ImageState.REGISTERED.value

# Config-owned fields the upsert overlays. Runtime-owned object_key/digest/state are written
# only by realization below (and never downgraded), so they are absent from this set.
_CONFIG_FIELDS = ("format", "root_device", "visibility", "capabilities")


@runtime_checkable
class ImageHeadStore(Protocol):
    """The narrow object-store port the image reconcile consumes (presence check only)."""

    def head_present(self, key: str) -> bool: ...


class _S3Head(Enum):
    """The resolved outcome of an ``s3`` object HEAD (or why it was not performed)."""

    NOT_S3 = auto()  # not an s3 source — no HEAD attempted
    NO_DIGEST = auto()  # s3 source without a digest — cannot register, no HEAD attempted
    PRESENT = auto()  # object confirmed present
    ABSENT = auto()  # object HEAD returned 404
    UNREACHABLE = auto()  # store unconfigured/unreachable (a connection error, not a 404)


async def _resolve_s3_head(
    entry: ImageEntry, row: dict[str, object] | None, store: ImageHeadStore
) -> _S3Head:
    """Resolve the s3 HEAD for an entry off the event loop, degrading on any store error.

    A non-``s3`` source, an already-realized row, or a digest-less ``s3`` source never HEADs
    (the digest is the registration gate). The blocking HEAD runs in a worker thread so a slow
    store never stalls the reconcile loop.
    """
    source = entry.source
    if not isinstance(source, S3Source):
        return _S3Head.NOT_S3
    if row is not None and row.get("state") == _REGISTERED:
        return _S3Head.NOT_S3  # realized row is preserved; no HEAD needed
    if source.digest is None:
        return _S3Head.NO_DIGEST
    try:
        present = await asyncio.to_thread(store.head_present, source.object_key)
    except CategorizedError:
        return _S3Head.UNREACHABLE
    return _S3Head.PRESENT if present else _S3Head.ABSENT


def _opt_str(row: dict[str, object], key: str) -> str | None:
    """Read an optional text column from a fetched row, narrowing ``object`` to ``str|None``."""
    value = row.get(key)
    assert value is None or isinstance(value, str)
    return value


def _entry_label(entry: ImageEntry) -> str:
    return f"image[{entry.provider}/{entry.name}/{entry.arch}]"


def _record(entry: ImageEntry, detail: str = "") -> ReconcileRecord:
    return ReconcileRecord(name=entry.name, entry=_entry_label(entry), detail=detail)


async def reconcile_images(
    conn: AsyncConnection, doc: InventoryDoc, store: ImageHeadStore
) -> ReconcileDiff:
    """Merge ``doc``'s images into ``image_catalog`` and prune departed config rows.

    Args:
        conn: The reconcile pass connection (the whole pass holds one session lock on it).
        doc: The parsed inventory document.
        store: The object store, used only to HEAD ``s3`` objects for existence.

    Returns:
        The :class:`ReconcileDiff` for this pass.
    """
    diff = ReconcileDiff()
    # Autocommit so the session lock and each pass transaction have clean boundaries: the
    # blocking upsert/prune transactions COMMIT on block exit (before pg_advisory_unlock), so
    # a second pass that unblocks on the lock observes this pass's committed rows and no-ops
    # instead of racing the identity constraint. Restored on exit.
    async with _autocommit(conn), inventory_pass_lock(conn):
        existing = await _load_config_rows(conn)
        await _upsert_entries(conn, doc, store, existing, diff)
        await _prune_departed(conn, doc, existing, diff)
    return diff


@asynccontextmanager
async def _autocommit(conn: AsyncConnection) -> AsyncIterator[None]:
    """Set ``conn`` to autocommit for the block, restoring the prior mode on exit."""
    previous = conn.autocommit
    if not previous:
        await conn.set_autocommit(True)
    try:
        yield
    finally:
        if not previous:
            await conn.set_autocommit(False)


async def _load_config_rows(
    conn: AsyncConnection,
) -> dict[tuple[str, str, str], dict[str, object]]:
    """Load config-owned rows keyed by ``(provider, name, arch)`` (the upsert/prune scope)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, provider, name, arch, format, root_device, visibility, capabilities, "
            "       object_key, digest, volume, state "
            "FROM image_catalog WHERE managed_by = %s",
            (_CONFIG,),
        )
        rows = await cur.fetchall()
    return {(r["provider"], r["name"], r["arch"]): r for r in rows}


async def _upsert_entries(
    conn: AsyncConnection,
    doc: InventoryDoc,
    store: ImageHeadStore,
    existing: dict[tuple[str, str, str], dict[str, object]],
    diff: ReconcileDiff,
) -> None:
    """Create/update config rows for each declared image, in one batched transaction."""
    async with conn.transaction():
        for entry in doc.image:
            row = existing.get(entry.identity)
            if row is None:
                await _create_entry(conn, entry, store, diff)
            else:
                await _update_entry(conn, entry, row, store, diff)


async def _create_entry(
    conn: AsyncConnection, entry: ImageEntry, store: ImageHeadStore, diff: ReconcileDiff
) -> None:
    """Insert a new config row, realizing it per its source kind."""
    head = await _resolve_s3_head(entry, None, store)
    state, object_key, volume, digest, warning = _realize(entry, None, head)
    await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, visibility, capabilities, "
        " object_key, volume, digest, state, managed_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            entry.provider,
            entry.name,
            entry.arch,
            entry.format,
            entry.root_device,
            entry.visibility,
            entry.capabilities,
            object_key,
            volume,
            digest,
            state,
            _CONFIG,
        ),
    )
    diff.created.append(_record(entry))
    if warning is not None:
        diff.warned.append(_record(entry, warning))


async def _update_entry(
    conn: AsyncConnection,
    entry: ImageEntry,
    row: dict[str, object],
    store: ImageHeadStore,
    diff: ReconcileDiff,
) -> None:
    """Change-detecting update of an existing config row's config-owned + realized fields."""
    desired = {
        "format": entry.format,
        "root_device": entry.root_device,
        "visibility": entry.visibility,
        "capabilities": list(entry.capabilities),
    }
    head = await _resolve_s3_head(entry, row, store)
    state, object_key, volume, digest, warning = _realize(entry, row, head)
    realized = {"object_key": object_key, "volume": volume, "digest": digest, "state": state}

    config_changed = any(row.get(k) != v for k, v in desired.items())
    realized_changed = any(row.get(k) != v for k, v in realized.items())
    if config_changed or realized_changed:
        await conn.execute(
            "UPDATE image_catalog SET format = %s, root_device = %s, visibility = %s, "
            "capabilities = %s, object_key = %s, volume = %s, digest = %s, state = %s "
            "WHERE id = %s",
            (
                desired["format"],
                desired["root_device"],
                desired["visibility"],
                desired["capabilities"],
                object_key,
                volume,
                digest,
                state,
                row["id"],
            ),
        )
        diff.updated.append(_record(entry))
    if warning is not None:
        diff.warned.append(_record(entry, warning))


def _realize(
    entry: ImageEntry, row: dict[str, object] | None, head: _S3Head
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """Compute ``(state, object_key, volume, digest, warning)`` for an entry.

    Never downgrades a row already ``registered`` from a build/upload: a ``build`` (or an
    ``s3`` whose object/digest is not yet confirmed) leaves a realized row exactly as it is,
    so the runtime-owned object_key/digest/state are preserved (invariant 1).
    """
    source = entry.source
    if isinstance(source, StagedSource):
        return (_REGISTERED, None, source.volume, None, None)
    if isinstance(source, BuildSource):
        return _realize_build(entry, row)
    if isinstance(source, S3Source):
        return _realize_s3(entry, row, source, head)
    raise AssertionError(f"unhandled image source kind: {source!r}")  # pragma: no cover


def _realize_build(
    entry: ImageEntry, row: dict[str, object] | None
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """A ``build`` source: keep a realized row as-is; else a ``defined`` placeholder + warn."""
    if row is not None and row.get("state") == _REGISTERED:
        return (
            _REGISTERED,
            _opt_str(row, "object_key"),
            _opt_str(row, "volume"),
            _opt_str(row, "digest"),
            None,
        )
    return (
        _DEFINED,
        None,
        None,
        None,
        f"{entry.name}: build source not yet realized; row stays defined until a build runs",
    )


def _realize_s3(
    entry: ImageEntry,
    row: dict[str, object] | None,
    source: S3Source,
    head: _S3Head,
) -> tuple[str, str | None, str | None, str | None, str | None]:
    """An ``s3`` source: registered only with a digest AND a confirmed-present object.

    A missing digest, a missing object (404), or an unreachable/unconfigured store all leave
    the row ``defined`` + warn — the pass still succeeds and realizes on a later reconcile.
    """
    if row is not None and row.get("state") == _REGISTERED:
        return (
            _REGISTERED,
            _opt_str(row, "object_key"),
            _opt_str(row, "volume"),
            _opt_str(row, "digest"),
            None,
        )
    if head is _S3Head.NO_DIGEST:
        return (
            _DEFINED,
            None,
            None,
            None,
            f"{entry.name}: s3 source has no digest; row stays defined (cannot register)",
        )
    if head is _S3Head.UNREACHABLE:
        return (
            _DEFINED,
            None,
            None,
            None,
            f"{entry.name}: object store unreachable; row stays defined until s3 is up",
        )
    if head is _S3Head.ABSENT:
        return (
            _DEFINED,
            None,
            None,
            None,
            f"{entry.name}: s3 object {source.object_key} absent; row stays defined",
        )
    return (_REGISTERED, source.object_key, None, source.digest, None)


async def _prune_departed(
    conn: AsyncConnection,
    doc: InventoryDoc,
    existing: dict[tuple[str, str, str], dict[str, object]],
    diff: ReconcileDiff,
) -> None:
    """Prune (or cordon) each config row whose identity left the file."""
    declared = {entry.identity for entry in doc.image}
    for identity, row in existing.items():
        if identity in declared:
            continue
        name = str(row["name"])
        outcome = await prune_or_cordon_image(conn, _row_id(row), name)
        entry = ReconcileRecord(
            name=name, entry=f"image[{identity[0]}/{identity[1]}/{identity[2]}]"
        )
        if outcome.cordoned:
            diff.cordoned.append(entry)
            _log.info("inventory: config image %s still in use; cordoned (not pruned)", name)
        elif outcome.pruned:
            diff.pruned.append(entry)
            _log.info("inventory: config image %s absent from config; row pruned", name)


def _row_id(row: dict[str, object]) -> UUID:
    value = row["id"]
    assert isinstance(value, UUID)
    return value
