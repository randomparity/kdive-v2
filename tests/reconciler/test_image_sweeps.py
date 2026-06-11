"""Tests for the reconciler image sweeps (M2.4/6, ADR-0092/0093, issue #287).

Three deadline-guarded sweeps modeled on the upload reaper:

* ``repair_leaked_images`` — an object under the image prefix with **no catalog row**,
  older than the publish grace (keyed off the object's store mtime), is deleted. A
  ``pending`` row inside its deadline protects its object (the row-first publish window).
* ``repair_dangling_images`` — a row whose object HEAD is missing **past its publish
  deadline** has its row removed; an object-less ``defined`` baseline is skipped (it is
  object-less by design, not dangling).
* ``repair_expired_private_images`` — a ``private`` row with ``expires_at < now()`` has
  its object + row deleted, but is **reference-guarded** (an image a non-terminal System
  still references via ``provisioning_profile`` is skipped) and **extend-fenced** (the
  ``expires_at`` is re-read under a per-row lock so a concurrent extend is honored).

Seeding uses an autocommit ``connect`` connection; repairs run through a real
non-autocommit pool via ``run_repair`` (mirroring ``test_upload_reaper.py``). All time
windows are set in SQL against the Postgres clock, so there is no test-vs-DB skew.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.reconciler.images import (
    ImageMtime,
)
from kdive.reconciler.images import (
    expire_one_private_image as _expire_one_private_image,
)
from kdive.reconciler.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.images import (
    repair_expired_private_images as _repair_expired_private_images,
)
from kdive.reconciler.images import (
    repair_leaked_images as _repair_leaked_images,
)
from tests.reconciler.conftest import connect, run_repair, seed_system


class _FakeImageStore:
    """A narrow image-sweep store stand-in (structural match for the repair port).

    ``objects`` maps object key -> age (how long ago the object was written). ``head``
    reports presence; ``list_image_objects`` reports each key with a Postgres-relative
    ``ImageMtime`` so the leaked-grace comparison stays on the DB clock.
    """

    def __init__(self, objects: dict[str, timedelta]) -> None:
        # objects maps key -> age; the absolute mtime is now - age.
        self._objects = dict(objects)
        self.deleted: list[str] = []

    def list_image_objects(self) -> list[ImageMtime]:
        now = datetime.now(UTC)
        return [
            ImageMtime(key=key, last_modified=now - age)
            for key, age in self._objects.items()
            if key not in self.deleted
        ]

    def head_present(self, key: str) -> bool:
        return key in self._objects and key not in self.deleted

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _insert_image_row(
    conn: psycopg.AsyncConnection,
    *,
    provider: str = "local-libvirt",
    name: str = "debian",
    arch: str = "x86_64",
    state: str = "registered",
    visibility: str = "public",
    object_key: str | None = "images/local-libvirt/debian/x86_64.qcow2",
    owner: str | None = None,
    pending_age: timedelta = timedelta(hours=2),
    expires_in: timedelta | None = None,
) -> UUID:
    """Insert one image_catalog row with DB-clock-relative pending_since/expires_at."""
    expires_clause = "now() + make_interval(secs => %(expires_secs)s)" if expires_in else "NULL"
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " expires_at, state, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, 'qcow2', '/dev/vda', %(object_key)s, "
        " %(digest)s, %(visibility)s, %(owner)s, "
        f"{expires_clause}, %(state)s, now() - make_interval(secs => %(pending_secs)s)) "
        "RETURNING id",
        {
            "provider": provider,
            "name": name,
            "arch": arch,
            "object_key": object_key,
            "digest": None if object_key is None else "sha256:abc",
            "visibility": visibility,
            "owner": owner,
            "state": state,
            "pending_secs": pending_age.total_seconds(),
            "expires_secs": (expires_in or timedelta()).total_seconds(),
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _set_catalog_rootfs(
    conn: psycopg.AsyncConnection, system_id: UUID, *, provider: str, name: str
) -> None:
    """Give a System a catalog-rootfs provisioning_profile referencing ``(provider, name)``."""
    profile = {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "kexec",
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "catalog", "provider": provider, "name": name}}
        },
    }
    await conn.execute(
        "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
        (Jsonb(profile), system_id),
    )


def _grace() -> timedelta:
    return timedelta(hours=1)


# --- leaked_images -------------------------------------------------------------------


def test_leaked_object_past_grace_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/orphan/x86_64.qcow2"
        store = _FakeImageStore({key: timedelta(hours=2)})  # older than 1h grace, no row
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 1
        assert store.deleted == [key]

    asyncio.run(_run())


def test_leaked_object_inside_grace_is_protected(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/fresh/x86_64.qcow2"
        store = _FakeImageStore({key: timedelta(minutes=5)})  # inside 1h grace
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 0
        assert store.deleted == []

    asyncio.run(_run())


def test_pending_row_inside_deadline_protects_its_object(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/pub/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            # A pending publish in flight: row exists, written recently (inside grace).
            await _insert_image_row(
                seed,
                name="pub",
                state="pending",
                object_key=key,
                pending_age=timedelta(minutes=1),
            )
        store = _FakeImageStore({key: timedelta(hours=5)})  # object old, but a row owns it
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_leaked_images(c, store, _grace()))
        assert count == 0
        assert store.deleted == []

    asyncio.run(_run())


# --- dangling_images -----------------------------------------------------------------


def test_dangling_row_with_missing_object_past_deadline_is_removed(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/gone/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed, name="gone", object_key=key, pending_age=timedelta(hours=2)
            )
        store = _FakeImageStore({})  # object HEAD missing
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 1
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_object_less_defined_row_is_skipped(migrated_url: str) -> None:
    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="baseline",
                state="defined",
                object_key=None,
                pending_age=timedelta(hours=5),
            )
        store = _FakeImageStore({})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None  # defined baseline survives

    asyncio.run(_run())


def test_dangling_row_inside_deadline_is_left_alone(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/young/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="young",
                state="pending",
                object_key=key,
                pending_age=timedelta(minutes=1),
            )
        store = _FakeImageStore({})  # object not landed yet, but inside deadline
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_dangling_skips_row_whose_object_is_present(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/healthy/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed, name="healthy", object_key=key, pending_age=timedelta(hours=2)
            )
        store = _FakeImageStore({key: timedelta(hours=2)})  # object present
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_dangling_images(c, store, _grace()))
        assert count == 0
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


# --- expired_private_images ----------------------------------------------------------


def test_expired_private_image_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/priv/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="priv",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),  # already expired
            )
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 1
        assert store.deleted == [key]
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_unexpired_private_image_is_kept(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/live/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="live",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(hours=1),  # still live
            )
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_expired_private_referenced_by_non_terminal_system_is_skipped(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/used/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="used",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            system_id = await seed_system(seed)  # READY, non-terminal
            await _set_catalog_rootfs(seed, system_id, provider="local-libvirt", name="used")
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 0
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None  # reference defers expiry

    asyncio.run(_run())


def test_expired_private_referenced_by_terminal_system_is_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        from kdive.domain.state import SystemState

        key = "images/local-libvirt__proj/dead/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_image_row(
                seed,
                name="dead",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            system_id = await seed_system(seed, system_state=SystemState.TORN_DOWN)
            await _set_catalog_rootfs(seed, system_id, provider="local-libvirt", name="dead")
        store = _FakeImageStore({key: timedelta(hours=2)})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: _repair_expired_private_images(c, store))
        assert count == 1  # a terminal System does not defer expiry
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is None

    asyncio.run(_run())


def test_concurrent_extend_under_lock_is_honored(migrated_url: str) -> None:
    """A candidate selected as expired but extended before the locked re-read is not deleted."""

    async def _run() -> None:
        key = "images/local-libvirt__proj/extended/x86_64.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="extended",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),  # candidate-eligible
            )
            # Simulate a concurrent operator extend committed before the per-row re-read.
            await conn.execute(
                "UPDATE image_catalog SET expires_at = now() + make_interval(hours => 1) "
                "WHERE id = %s",
                (row_id,),
            )
            store = _FakeImageStore({key: timedelta(hours=2)})
            deleted = await _expire_one_private_image(conn, store, row_id, key)
        assert deleted is False  # the re-read observes the extend
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            cur = await check.execute("SELECT 1 FROM image_catalog WHERE id = %s", (row_id,))
            assert await cur.fetchone() is not None

    asyncio.run(_run())


def test_expire_one_private_image_deletes_when_still_expired(migrated_url: str) -> None:
    async def _run() -> None:
        key = "images/local-libvirt__proj/stale/x86_64.qcow2"
        async with await connect(migrated_url) as conn:
            row_id = await _insert_image_row(
                conn,
                name="stale",
                visibility="private",
                owner="proj",
                object_key=key,
                expires_in=timedelta(seconds=-1),
            )
            store = _FakeImageStore({key: timedelta(hours=2)})
            deleted = await _expire_one_private_image(conn, store, row_id, key)
        assert deleted is True
        assert store.deleted == [key]

    asyncio.run(_run())
