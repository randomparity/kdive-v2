"""Integration tests for the inventory reconcile engine (M2.6 #390, ADR-0112).

Exercises ``reconcile_images`` against a disposable migrated Postgres (ADR-0019) plus a
narrow fake object store. Each test encodes one spec invariant from plan Task 1.4:

* 1 — never overwrites a build-realized row's runtime-owned ``object_key``/``digest``/``state``;
* 2/3 — prune touches only ``managed_by='config'`` rows (runtime/private untouched);
* 5 — prune of an in-use image cordons (does not delete the row);
* 7 — the relaxed ``image_object_present`` CHECK rejects both/neither object_key+volume;
* 8 — an ``s3`` source without a digest stays ``defined`` + warns.

Plus: idempotency (a second pass is a clean no-op), the s3 store-unreachable degrade (the
row stays ``defined`` and the pass succeeds rather than aborting), the kind-aware cordon
guard (a live **remote** System on a staged base image cordons, not deletes — Task 1.5,
load-bearing now that ``repair_leaked_images`` GCs an orphaned object after the row is gone),
and the concurrent-pass serialization (two passes do not abort on the identity constraint).

Prune is **row-delete-only** (ADR-0112): reconcile never calls ``store.delete`` — orphaned
objects are reclaimed by the existing ``repair_leaked_images`` reconciler sweep. The fake
store therefore records no ``delete`` calls from a reconcile pass; an asserted empty
``deleted`` list is the regression guard against re-introducing inline reclaim.

Seeding uses an autocommit connection (each insert self-commits); reconcile runs on a
non-autocommit pool connection so the real transaction framing is exercised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.loader import load_inventory
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_images import reconcile_images

# `migrated_url` is provided as a fixture by tests/integration/conftest.py (re-exported from
# tests.db.conftest), resolved by pytest at call time — no import (avoids the F811 shadow).

# --- fakes / helpers -----------------------------------------------------------------


class _FakeImageStore:
    """A narrow object-store stand-in (structural match for the reconcile store port).

    ``present`` is the set of keys a HEAD reports as existing. ``unreachable=True`` makes
    every ``head_present`` raise the infrastructure error a real store throws when the
    bucket is unconfigured/unreachable (a connection failure, not a clean 404). ``deleted``
    records ``delete`` calls — reconcile must never append to it (prune is row-delete-only).
    """

    def __init__(self, present: set[str] | None = None, *, unreachable: bool = False) -> None:
        self._present = set(present or ())
        self._unreachable = unreachable
        self.deleted: list[str] = []

    def head_present(self, key: str) -> bool:
        if self._unreachable:
            raise CategorizedError(
                "object store unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return key in self._present

    def delete(self, key: str) -> None:  # pragma: no cover - asserted never called
        self.deleted.append(key)


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(body)
    return path


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def _one(conn: psycopg.AsyncConnection, name: str) -> dict[str, object]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM image_catalog WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None, f"no image_catalog row named {name!r}"
    return row


async def _exists(conn: psycopg.AsyncConnection, name: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM image_catalog WHERE name = %s", (name,))
        return await cur.fetchone() is not None


async def _insert_registered_build_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    object_key: str,
    digest: str,
    provider: str = "local-libvirt",
    arch: str = "x86_64",
    managed_by: str = "config",
    visibility: str = "public",
    owner: str | None = None,
) -> UUID:
    """Insert a build-realized ``registered`` row (object_key + digest set)."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " state, managed_by, expires_at) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, %s, %s, %s, 'registered', %s, %s) "
        "RETURNING id",
        (
            provider,
            name,
            arch,
            object_key,
            digest,
            visibility,
            owner,
            managed_by,
            None if visibility == "public" else "now() + interval '1 day'",
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_config_staged_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    volume: str,
    provider: str = "remote-libvirt",
    arch: str = "x86_64",
) -> UUID:
    """Insert a config-owned ``registered`` staged row (volume set, object_key NULL)."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, volume, visibility, state, managed_by) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, 'public', 'registered', 'config') "
        "RETURNING id",
        (provider, name, arch, volume),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_private_upload_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    object_key: str,
    provider: str = "local-libvirt",
    arch: str = "x86_64",
    owner: str = "proj",
) -> UUID:
    """Insert a runtime-owned project-private upload sharing an identity with a config image."""
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " expires_at, state, managed_by) "
        "VALUES (%s, %s, %s, 'qcow2', '/dev/vda', %s, 'sha256:priv', 'private', %s, "
        " now() + interval '1 day', 'registered', 'runtime') "
        "RETURNING id",
        (provider, name, arch, object_key, owner),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _seed_non_terminal_system(
    conn: psycopg.AsyncConnection, *, provisioning_profile: dict[str, object]
) -> UUID:
    """Insert resource -> allocation -> READY system with the given provisioning_profile."""
    resource_id = uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    allocation_id = uuid4()
    await conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (%s, 'alice', 'proj', %s, 'active')",
        (allocation_id, resource_id),
    )
    system_id = uuid4()
    await conn.execute(
        "INSERT INTO systems (id, principal, project, allocation_id, state, provisioning_profile) "
        "VALUES (%s, 'alice', 'proj', %s, 'ready', %s)",
        (system_id, allocation_id, Jsonb(provisioning_profile)),
    )
    return system_id


def _local_catalog_profile(provider: str, name: str) -> dict[str, object]:
    return {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "direct-kernel",
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "catalog", "provider": provider, "name": name}}
        },
    }


def _remote_base_volume_profile(volume: str) -> dict[str, object]:
    return {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "disk-image",
        "provider": {"remote-libvirt": {"base_image_volume": volume}},
    }


# --- tests ---------------------------------------------------------------------------


def test_staged_image_registers_with_volume(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "remote-libvirt"\n'
                'name = "base"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "staged"\n'
                'volume = "base.qcow2"\n',
            )
        )
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "base")
        assert row["state"] == "registered"
        assert row["volume"] == "base.qcow2"
        assert row["object_key"] is None
        assert row["managed_by"] == "config"
        assert "base" in {c.name for c in diff.created}
        assert store.deleted == []

    asyncio.run(_run())


def test_s3_image_without_digest_stays_defined(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                'object_key = "images/local-libvirt/i/x86_64.qcow2"\n',
            )
        )
        store = _FakeImageStore(present={"images/local-libvirt/i/x86_64.qcow2"})
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "defined"
        assert row["object_key"] is None
        assert any("i" in w.entry for w in diff.warned)

    asyncio.run(_run())


def test_s3_with_digest_and_present_object_registers(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        key = "images/local-libvirt/i/x86_64.qcow2"
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                f'object_key = "{key}"\n'
                'digest = "sha256:beef"\n',
            )
        )
        store = _FakeImageStore(present={key})
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "registered"
        assert row["object_key"] == key
        assert row["digest"] == "sha256:beef"

    asyncio.run(_run())


def test_s3_store_unreachable_degrades_to_defined(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "i"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "s3"\n'
                'object_key = "images/local-libvirt/i/x86_64.qcow2"\n'
                'digest = "sha256:beef"\n',
            )
        )
        store = _FakeImageStore(unreachable=True)
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)  # must not raise
        async with await _connect(migrated_url) as check:
            row = await _one(check, "i")
        assert row["state"] == "defined"  # degraded, not aborted
        assert any("i" in w.entry for w in diff.warned)

    asyncio.run(_run())


def test_reconcile_never_overwrites_realized_object_key(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_registered_build_row(
                seed,
                name="built",
                object_key="images/local-libvirt/built/x86_64.qcow2",
                digest="sha256:dead",
            )
        doc = load_inventory(
            _write_toml(
                tmp_path,
                "schema_version = 2\n"
                "[[image]]\n"
                'provider = "local-libvirt"\n'
                'name = "built"\n'
                'arch = "x86_64"\n'
                'format = "qcow2"\n'
                'root_device = "/dev/vda"\n'
                'visibility = "public"\n'
                "[image.source]\n"
                'kind = "build"\n'
                'base = "fedora-43"\n',
            )
        )
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            row = await _one(check, "built")
        assert row["state"] == "registered"  # NOT downgraded to defined
        assert row["object_key"] == "images/local-libvirt/built/x86_64.qcow2"
        assert row["digest"] == "sha256:dead"
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_removes_only_config_rows_absent_from_config(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_registered_build_row(
                seed,
                name="runtime-img",
                object_key="images/local-libvirt/runtime-img/x86_64.qcow2",
                digest="sha256:1",
                managed_by="runtime",
            )
            await _insert_config_staged_row(seed, name="stale-config", volume="v.qcow2")
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))  # nothing declared
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "runtime-img")  # runtime row untouched
            assert not await _exists(check, "stale-config")  # config row pruned (idle)
        assert "stale-config" in {p.name for p in diff.pruned}
        assert store.deleted == []  # row-delete-only; GC reclaims any object

    asyncio.run(_run())


def test_prune_skips_private_upload_sharing_identity(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        # A config image and a project-private upload share (provider,name,arch); the empty
        # config must prune the config row but leave the private upload untouched.
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="shared", volume="v.qcow2", provider="local-libvirt"
            )
            await _insert_private_upload_row(
                seed,
                name="shared",
                object_key="images/local-libvirt__proj/shared/x86_64.qcow2",
                provider="local-libvirt",
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT visibility, managed_by FROM image_catalog WHERE name = 'shared'"
            )
            rows = await cur.fetchall()
        kinds = {(r["visibility"], r["managed_by"]) for r in rows}
        assert kinds == {("private", "runtime")}  # config row pruned, private upload kept
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_of_in_use_image_cordons_not_deletes(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="busy", volume="v.qcow2", provider="local-libvirt"
            )
            await _seed_non_terminal_system(
                seed,
                provisioning_profile=_local_catalog_profile("local-libvirt", "busy"),
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "busy")  # NOT deleted
        assert "busy" in {c.name for c in diff.cordoned}
        assert "busy" not in {p.name for p in diff.pruned}
        assert store.deleted == []

    asyncio.run(_run())


def test_prune_of_in_use_remote_staged_image_cordons_not_deletes(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 1.5: a live REMOTE System references its base image by base_image_volume (the
    # image's `volume`), NOT by (provider,name) catalog ref. The generalized guard must
    # cordon it; deleting the row would let repair_leaked_images GC nothing here (staged has
    # no object) but the same path for an s3 remote base would lose bytes.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_config_staged_row(
                seed, name="remote-base", volume="base.qcow2", provider="remote-libvirt"
            )
            await _seed_non_terminal_system(
                seed,
                provisioning_profile=_remote_base_volume_profile("base.qcow2"),
            )
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_images(conn, doc, store)
        async with await _connect(migrated_url) as check:
            assert await _exists(check, "remote-base")  # cordoned, not deleted
        assert "remote-base" in {c.name for c in diff.cordoned}
        assert store.deleted == []

    asyncio.run(_run())


def test_relaxed_check_rejects_both_or_neither(migrated_url: str) -> None:
    async def _run() -> None:
        async def _raw_insert(
            conn: psycopg.AsyncConnection,
            *,
            state: str,
            object_key: str | None,
            volume: str | None,
            name: str,
        ) -> None:
            await conn.execute(
                "INSERT INTO image_catalog "
                "(provider, name, arch, format, root_device, object_key, volume, visibility, "
                " state, managed_by, digest) "
                "VALUES ('p', %s, 'x86_64', 'qcow2', '/dev/vda', %s, %s, 'public', %s, "
                " 'config', %s)",
                (
                    name,
                    object_key,
                    volume,
                    state,
                    None if state == "defined" else "sha256:x",
                ),
            )

        async with await _connect(migrated_url) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):  # both
                await _raw_insert(conn, state="registered", object_key="k", volume="v", name="both")
            with pytest.raises(psycopg.errors.CheckViolation):  # neither
                await _raw_insert(
                    conn, state="registered", object_key=None, volume=None, name="neither"
                )
            with pytest.raises(psycopg.errors.CheckViolation):  # defined w/ key
                await _raw_insert(
                    conn, state="defined", object_key="k", volume=None, name="def-key"
                )
            # valid shapes succeed:
            await _raw_insert(conn, state="registered", object_key="k", volume=None, name="ok-key")
            await _raw_insert(conn, state="registered", object_key=None, volume="v", name="ok-vol")

    asyncio.run(_run())


def test_reconcile_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        body = (
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "base.qcow2"\n'
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        store = _FakeImageStore()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_images(conn, doc, store)
            async with pool.connection() as conn:
                diff2 = await reconcile_images(conn, doc, store)
        assert not diff2.created
        assert not diff2.updated
        assert not diff2.pruned

    asyncio.run(_run())


def test_concurrent_passes_do_not_abort_on_identity(migrated_url: str, tmp_path: Path) -> None:
    # Two reconcile passes in flight must serialize on the session inventory lock: no
    # unique-violation abort, and the second is a clean no-op.
    async def _run() -> None:
        body = (
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "base.qcow2"\n'
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        store = _FakeImageStore()
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool:

            async def _pass() -> ReconcileDiff:
                async with pool.connection() as conn:
                    return await reconcile_images(conn, doc, store)

            diffs = await asyncio.gather(_pass(), _pass())
        created_total = sum(len(d.created) for d in diffs)
        assert created_total == 1  # exactly one pass created the row; the other no-ops
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute("SELECT count(*) FROM image_catalog WHERE name = 'base'")
            row = await cur.fetchone()
        assert row is not None and row[0] == 1

    asyncio.run(_run())
