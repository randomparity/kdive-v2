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
from typing import Any, cast
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
from kdive.inventory.reconcile_resources import reconcile_resources
from kdive.provider_components.artifacts import ObjectListing
from kdive.providers.reaping import NullReaper
from kdive.reconciler.inventory import InventoryReconcilePass
from kdive.reconciler.loop import ReconcileConfig, reconcile_once

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

    def list_image_objects(self) -> list[ObjectListing]:
        # Empty so the loop's sibling image sweeps (leaked/dangling) are clean no-ops when
        # this fake is used as the full ImageSweepStore in the loop-config tests.
        return []

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


def test_reconcile_rejects_connection_with_open_transaction(
    migrated_url: str, tmp_path: Path
) -> None:
    # The pass toggles autocommit + holds a session lock across transactions, so it must own a
    # transaction-free connection; calling it inside an open transaction fails fast with a
    # clear error rather than psycopg's opaque ProgrammingError.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        store = _FakeImageStore()
        conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=False)
        try:
            async with conn.transaction():
                await conn.execute("SELECT 1")  # force an open transaction
                with pytest.raises(RuntimeError, match="no open transaction"):
                    await reconcile_images(conn, doc, store)
        finally:
            await conn.close()

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


# --- loop pass: fault isolation (plan Task 1.6) --------------------------------------


def _config_with_inventory_spec() -> ReconcileConfig:
    """A reconcile config that wires an image store, so the inventory pass is in the plan.

    The inventory pass needs an :class:`ImageHeadStore`; the loop only adds the spec when
    ``image_store`` is set (mirroring the image-sweep specs), so the fault-isolation tests
    must hand one in for the ``reconcile_inventory`` pass to run at all.
    """
    return ReconcileConfig(image_store=_FakeImageStore())


def test_loop_inventory_pass_is_fault_isolated(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A malformed systems.toml must NOT abort sibling reaper repairs: the inventory pass is
    # recorded in report.failures while every other repair in the plan still ran (loop.py
    # 350-356 contract). An inventory failure must never raise out of reconcile_once.
    async def _run() -> None:
        bad = tmp_path / "systems.toml"
        bad.write_text("schema_version = 2\n[[image]\n")  # malformed TOML
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(bad))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" in report.failures  # this pass failed
        assert report.reaped_active_allocations >= 0  # siblings still ran

    asyncio.run(_run())


def test_loop_inventory_pass_skips_quietly_when_default_file_absent(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # systems.toml is gitignored; an absent DEFAULT file is the normal pre-config state and
    # must NOT mark the pass failed every loop iteration.
    async def _run() -> None:
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "does-not-exist.toml"))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" not in report.failures  # absent default != failure

    asyncio.run(_run())


def test_loop_inventory_pass_reconciles_a_present_file(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A present, valid file is reconciled into the catalog as a sibling repair: the config row
    # is created and the pass is not a failure.
    async def _run() -> None:
        path = _write_toml(
            tmp_path,
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "loop-base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "loop-base.qcow2"\n',
        )
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" not in report.failures
        async with await _connect(migrated_url) as check:
            row = await _one(check, "loop-base")
        assert row["state"] == "registered"
        assert row["managed_by"] == "config"

    asyncio.run(_run())


def test_loop_inventory_pass_absent_when_no_image_store(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With no image store the inventory pass cannot run (it needs the store to HEAD s3
    # objects), so even a malformed file is a no-op for the loop — the spec is simply absent.
    async def _run() -> None:
        bad = tmp_path / "systems.toml"
        bad.write_text("schema_version = 2\n[[image]\n")
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(bad))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())  # default config: no image store
        assert "reconcile_inventory" not in report.failures

    asyncio.run(_run())


def test_loop_inventory_pass_unreadable_file_is_fault_isolated(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A present-but-unreadable path (here a directory at the configured path) must surface as a
    # failed-this-pass spec via the loader's OSError->InventoryError wrap, not crash the pass —
    # the hash-read fast path catches OSError and defers to the loader.
    async def _run() -> None:
        as_dir = tmp_path / "systems.toml"
        as_dir.mkdir()  # a directory: read_bytes raises IsADirectoryError (an OSError)
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(as_dir))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" in report.failures

    asyncio.run(_run())


def test_inventory_pass_repairs_drift_on_unchanged_file(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-0021 drift repair must NOT be gated on the file hash: a config-owned row manually
    # deleted out from under an UNCHANGED systems.toml is re-created on the next pass. The
    # content-hash cache may skip only the parse step; the reconcile-against-DB step runs every
    # pass. The file's mtime/bytes never change between the two passes (a cache hit), so a
    # re-created row proves the reconcile step is not skipped on a cache hit.
    async def _run() -> None:
        path = _write_toml(
            tmp_path,
            "schema_version = 2\n"
            "[[image]]\n"
            'provider = "remote-libvirt"\n'
            'name = "drift-base"\n'
            'arch = "x86_64"\n'
            'format = "qcow2"\n'
            'root_device = "/dev/vda"\n'
            'visibility = "public"\n'
            "[image.source]\n"
            'kind = "staged"\n'
            'volume = "drift-base.qcow2"\n',
        )
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        store = _FakeImageStore()
        pass_ = InventoryReconcilePass()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            async with pool.connection() as conn:
                created = await pass_.run(conn, store)
            assert created == 1  # first pass creates + caches the parse by hash
            async with await _connect(migrated_url) as drift:
                await drift.execute("DELETE FROM image_catalog WHERE name = 'drift-base'")
            async with pool.connection() as conn:
                repaired = await pass_.run(conn, store)  # same file → cache hit, must still repair
            assert repaired == 1  # the deleted config row is re-created (drift repaired)
            async with await _connect(migrated_url) as check:
                assert await _exists(check, "drift-base")

    asyncio.run(_run())


# --- resources: config overlay + #385 (Phase 2, plan Tasks 2.1-2.3) ------------------
#
# These exercise reconcile_resources against a disposable migrated Postgres. The contract:
# managed_by governs existence (config for fault_inject/remote_libvirt, discovery for
# host-probed local-libvirt); a config overlay writes cost_class to the COLUMN and
# vcpus/memory_mb/cap to the capabilities JSONB; prune is cordon-not-delete for a live row.


def _fault_inject_toml(
    *,
    name: str = "fi-1",
    cost_class: str = "local",
    vcpus: int = 8,
    memory_mb: int = 16384,
    cap: int = 1,
) -> str:
    return (
        "schema_version = 2\n"
        "[[fault_inject]]\n"
        f'name = "{name}"\n'
        f'cost_class = "{cost_class}"\n'
        f"vcpus = {vcpus}\n"
        f"memory_mb = {memory_mb}\n"
        f"concurrent_allocation_cap = {cap}\n"
    )


def _remote_libvirt_toml(*, name: str, base_image: str = "base") -> str:
    return (
        "schema_version = 2\n"
        "[[image]]\n"
        'provider = "remote-libvirt"\n'
        f'name = "{base_image}"\n'
        'arch = "x86_64"\n'
        'format = "qcow2"\n'
        'root_device = "/dev/vda"\n'
        'visibility = "public"\n'
        "[image.source]\n"
        'kind = "staged"\n'
        f'volume = "{base_image}.qcow2"\n'
        "[[remote_libvirt]]\n"
        f'name = "{name}"\n'
        'uri = "qemu+tls://h1/system"\n'
        'gdb_addr = "10.0.0.1"\n'
        'gdbstub_range = "47000:47099"\n'
        'client_cert_ref = "c.pem"\n'
        'client_key_ref = "k.pem"\n'  # pragma: allowlist secret - filename ref
        'ca_cert_ref = "ca.pem"\n'  # pragma: allowlist secret - filename ref
        f'base_image = "{base_image}"\n'
        'cost_class = "remote"\n'
        "concurrent_allocation_cap = 1\n"
    )


async def _resource_by_name(conn: psycopg.AsyncConnection, name: str) -> dict[str, object]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM resources WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None, f"no resources row named {name!r}"
    return row


def _row_caps(row: dict[str, object]) -> dict[str, Any]:
    """Narrow the jsonb ``capabilities`` column to a string-keyed dict for assertions."""
    caps = row["capabilities"]
    assert isinstance(caps, dict)
    return cast("dict[str, Any]", caps)


async def _resource_count(conn: psycopg.AsyncConnection, *, kind: str, host_uri: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM resources WHERE kind = %s AND host_uri = %s",
            (kind, host_uri),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _insert_discovered_local(
    conn: psycopg.AsyncConnection,
    *,
    host_uri: str = "qemu:///system",
    vcpus: int = 16,
    memory_mb: int = 65536,
) -> UUID:
    """Insert a discovery-owned local-libvirt row (the discovery/registrar insert path)."""
    rid = uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, host_uri, "
        " managed_by) "
        "VALUES (%s, 'local-libvirt', %s, 'local-libvirt', 'local', 'available', %s, 'discovery')",
        (rid, Jsonb({"vcpus": vcpus, "memory_mb": memory_mb, "pcie": ["0000:00:1f.0"]}), host_uri),
    )
    return rid


async def _seed_live_allocation_on(conn: psycopg.AsyncConnection, resource_id: UUID) -> None:
    """Attach a non-terminal (active) allocation so the resource counts as live."""
    await conn.execute(
        "INSERT INTO allocations (id, principal, project, resource_id, state) "
        "VALUES (%s, 'alice', 'proj', %s, 'active')",
        (uuid4(), resource_id),
    )


def test_fault_inject_overlay_lands_caps_in_jsonb_and_cost_class_in_column(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 1 (load-bearing): cost_class -> the COLUMN; vcpus/memory_mb/cap -> the JSONB.
    async def _run() -> None:
        doc = load_inventory(
            _write_toml(tmp_path, _fault_inject_toml(vcpus=8, memory_mb=16384, cap=3))
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-1")
        assert row["managed_by"] == "config"
        assert row["cost_class"] == "local"  # the COLUMN, not jsonb
        assert row["status"] == "available"
        assert row["pool"] == "default"
        caps = _row_caps(row)
        assert caps["vcpus"] == 8
        assert caps["memory_mb"] == 16384
        assert caps["concurrent_allocation_cap"] == 3
        assert "cost_class" not in caps  # never written into jsonb
        assert "fi-1" in {c.name for c in diff.created}

    asyncio.run(_run())


def test_fault_inject_resource_is_admitted_not_configuration_error(
    migrated_url: str, tmp_path: Path
) -> None:
    # #385 regression headline: after reconcile, allocations.request(kind=fault-inject) is
    # ADMITTED, not configuration_error (the vcpus=None denial the issue reports).
    from kdive.domain.models import ResourceKind
    from kdive.security.authz.context import RequestContext
    from kdive.services.allocation.request import AdmissionRequestSpec, request_admission

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(vcpus=8, memory_mb=16384)))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
                    "VALUES ('proj', 1000000, 0)"
                )
                await seed.execute(
                    "INSERT INTO quotas (project, max_concurrent_allocations, "
                    " max_concurrent_systems) VALUES ('proj', 100, 100)"
                )
            async with pool.connection() as conn:
                result = await request_admission(
                    conn,
                    RequestContext(principal="alice", agent_session="s", projects=("proj",)),
                    project="proj",
                    spec=AdmissionRequestSpec(
                        resource_id=None,
                        kind=ResourceKind.FAULT_INJECT,
                        shape="small",
                        vcpus=None,
                        memory_gb=None,
                        disk_gb=None,
                        window=None,
                        pcie_devices=(),
                        on_capacity="deny",
                    ),
                )
        assert result.error is None, f"unexpected error: {result.error}"
        assert result.category is None, f"unexpected denial category: {result.category}"
        assert result.allocation is not None, f"not admitted: {result.denial}"

    asyncio.run(_run())


def test_remote_libvirt_is_sole_creator_no_duplicate_with_legacy_discovery(
    migrated_url: str, tmp_path: Path
) -> None:
    # One-creator-per-kind (invariant 4): config + a legacy-discovery insert for one remote
    # host must converge to EXACTLY ONE row, never a duplicate / (kind,name) unique violation.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            # Legacy env-based discovery would bind-only (non-creating) in Phase 2; simulate a
            # second reconcile pass to prove idempotency does not create a second row.
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
        uri = "qemu+tls://h1/system"
        async with await _connect(migrated_url) as check:
            assert await _resource_count(check, kind="remote-libvirt", host_uri=uri) == 1

    asyncio.run(_run())


def test_remote_libvirt_adopts_legacy_discovery_row(migrated_url: str, tmp_path: Path) -> None:
    # A row the legacy env-based discovery already inserted (name NULL) for the same host_uri is
    # ADOPTED (flipped to config + named), not duplicated — the Phase-2→3 one-creator invariant.
    async def _run() -> None:
        uri = "qemu+tls://h1/system"
        async with await _connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO resources (id, kind, capabilities, pool, cost_class, status, "
                " host_uri, managed_by) "
                "VALUES (%s, 'remote-libvirt', %s, 'remote', 'remote', 'available', %s, "
                " 'discovery')",
                (uuid4(), Jsonb({"vcpus": 32, "memory_mb": 131072}), uri),
            )
        doc = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert await _resource_count(check, kind="remote-libvirt", host_uri=uri) == 1
            row = await _resource_by_name(check, "r1")
            assert row["managed_by"] == "config"  # adopted
            caps = _row_caps(row)
            assert caps["vcpus"] == 32  # discovery-contributed hardware fact preserved
            assert caps["memory_mb"] == 131072

    asyncio.run(_run())


def test_remote_libvirt_uri_change_updates_in_place_no_duplicate(
    migrated_url: str, tmp_path: Path
) -> None:
    # Regression: changing a declared remote host's uri (same name) must UPDATE the row in
    # place, not INSERT a second row that collides on the (kind, name) partial-unique index.
    async def _run() -> None:
        first = load_inventory(_write_toml(tmp_path, _remote_libvirt_toml(name="r1")))
        moved_body = _remote_libvirt_toml(name="r1").replace(
            "qemu+tls://h1/system", "qemu+tls://h2/system"
        )
        moved = load_inventory(_write_toml(tmp_path, moved_body))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, first)
            async with pool.connection() as conn:
                await reconcile_resources(conn, moved)  # must not raise a unique violation
        old_uri, new_uri = "qemu+tls://h1/system", "qemu+tls://h2/system"
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "r1")
            assert row["host_uri"] == new_uri  # uri change propagated
            assert await _resource_count(check, kind="remote-libvirt", host_uri=old_uri) == 0
            assert await _resource_count(check, kind="remote-libvirt", host_uri=new_uri) == 1

    asyncio.run(_run())


def test_local_libvirt_overlay_preserves_discovery_owned_hardware(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 2.3 + invariant (no-overwrite): a discovered local-libvirt row receives the cost/cap
    # overlay and inherits the config name WITHOUT its discovery-owned vcpus/memory/PCIe changing.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_discovered_local(
                seed, host_uri="qemu:///system", vcpus=16, memory_mb=65536
            )
        body = (
            "schema_version = 2\n"
            "[[local_libvirt]]\n"
            'name = "lab-host"\n'
            'host_uri = "qemu:///system"\n'
            'cost_class = "local"\n'
            "concurrent_allocation_cap = 4\n"
        )
        doc = load_inventory(_write_toml(tmp_path, body))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "lab-host")
        assert row["managed_by"] == "discovery"  # existence stays discovery-owned
        assert row["name"] == "lab-host"  # inherited from config
        assert row["cost_class"] == "local"
        caps = _row_caps(row)
        assert caps["vcpus"] == 16  # discovery-owned, NOT overwritten
        assert caps["memory_mb"] == 65536  # discovery-owned, NOT overwritten
        assert caps["pcie"] == ["0000:00:1f.0"]  # discovery-owned, NOT overwritten
        assert caps["concurrent_allocation_cap"] == 4  # overlaid from config

    asyncio.run(_run())


def test_discovered_local_with_no_config_gets_deterministic_name(
    migrated_url: str, tmp_path: Path
) -> None:
    # A discovered host with no config instance gets a deterministic name from host_uri and is
    # NOT pruned (it is discovery-owned, not config-owned).
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            rid = await _insert_discovered_local(seed, host_uri="qemu:///system")
        doc = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM resources WHERE id = %s", (rid,))
            row = await cur.fetchone()
        assert row is not None  # discovery-owned row never pruned
        assert row["managed_by"] == "discovery"
        assert row["name"] is not None and row["name"] != ""  # deterministic name assigned

    asyncio.run(_run())


def test_prune_removes_only_config_resources(migrated_url: str, tmp_path: Path) -> None:
    # Invariant 2/3 for resources: prune touches only managed_by='config' rows; a discovery
    # row and a runtime row sharing the field-space are untouched.
    async def _run() -> None:
        # First create a config fault-inject row, plus a discovery local row.
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-keep")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with await _connect(migrated_url) as seed:
                disc = await _insert_discovered_local(seed)
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            # Now reconcile an EMPTY doc: the config fault-inject row must be pruned, the
            # discovery row untouched.
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, empty)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 0
            )
            async with check.cursor() as cur:
                await cur.execute("SELECT 1 FROM resources WHERE id = %s", (disc,))
                assert await cur.fetchone() is not None  # discovery row survives
        assert "fi-keep" in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_prune_of_live_config_resource_cordons_not_deletes(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 5 for resources: a config resource with a live (non-terminal) allocation is
    # CORDONED, not deleted, when it leaves config.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-busy")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                busy = await _resource_by_name(seed, "fi-busy")
                busy_id = busy["id"]
                assert isinstance(busy_id, UUID)
                await _seed_live_allocation_on(seed, busy_id)
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_resources(conn, empty)
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-busy")  # still present
        assert row["cordoned"] is True
        assert "fi-busy" in {c.name for c in diff.cordoned}
        assert "fi-busy" not in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_reconcile_resources_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    # A second pass over an unchanged doc is a clean no-op (change-detecting upserts).
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with pool.connection() as conn:
                diff2 = await reconcile_resources(conn, doc)
        assert not diff2.created and not diff2.updated and not diff2.pruned

    asyncio.run(_run())


def test_discovery_insert_path_writes_managed_by_discovery(migrated_url: str) -> None:
    # Invariant 5 (load-bearing): a host discovered AFTER the migration must insert at
    # 'discovery', not the column default 'runtime'. Exercises the real registrar insert path.
    from kdive.domain.discovery import ResourceRecord
    from kdive.domain.models import ResourceKind
    from kdive.domain.state import ResourceStatus
    from kdive.services.resources.discovery import register_discovered_resource

    async def _run() -> None:
        record = ResourceRecord(
            resource_id="qemu:///system",
            kind=ResourceKind.LOCAL_LIBVIRT,
            capabilities={"vcpus": 8, "memory_mb": 8192},
            status=ResourceStatus.AVAILABLE,
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await register_discovered_resource(
                conn, record, pool="local-libvirt", cost_class="local"
            )
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT managed_by FROM resources WHERE host_uri = 'qemu:///system'")
            row = await cur.fetchone()
        assert row is not None
        assert row["managed_by"] == "discovery"  # NOT 'runtime' (the column default)

    asyncio.run(_run())


# --- Phase 3 Task 3.1: array-of-tables multi-instance --------------------------------


def _two_fault_inject_toml() -> str:
    # Two [[fault_inject]] instances sharing the synthetic host_uri (fault-inject://local),
    # distinguished only by their (kind, name) identity — the Phase-3 multi-instance goal.
    return (
        "schema_version = 2\n"
        "[[fault_inject]]\n"
        'name = "fi-a"\n'
        'cost_class = "local"\n'
        "vcpus = 4\n"
        "memory_mb = 4096\n"
        "[[fault_inject]]\n"
        'name = "fi-b"\n'
        'cost_class = "local"\n'
        "vcpus = 8\n"
        "memory_mb = 8192\n"
    )


def test_two_fault_inject_instances_reconcile_to_two_distinct_rows(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.1: N rows per kind. Two [[fault_inject]] sharing host_uri='fault-inject://local'
    # coexist via the (kind, name) unique index — two distinct rows, one per name.
    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _two_fault_inject_toml()))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check:
            assert (
                await _resource_count(check, kind="fault-inject", host_uri="fault-inject://local")
                == 2
            )
            row_a = await _resource_by_name(check, "fi-a")
            row_b = await _resource_by_name(check, "fi-b")
        assert row_a["id"] != row_b["id"]  # two distinct resource rows
        assert _row_caps(row_a)["vcpus"] == 4
        assert _row_caps(row_b)["vcpus"] == 8
        assert {"fi-a", "fi-b"} <= {c.name for c in diff.created}

    asyncio.run(_run())


def test_two_fault_inject_instances_are_each_independently_allocatable(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.1: both instances are independently allocatable. Targeting each by resource_id
    # admits an allocation on it — no allocation-API change, selection by resource_id.
    from kdive.domain.models import ResourceKind
    from kdive.security.authz.context import RequestContext
    from kdive.services.allocation.request import AdmissionRequestSpec, request_admission

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _two_fault_inject_toml()))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
                    "VALUES ('proj', 1000000, 0)"
                )
                await seed.execute(
                    "INSERT INTO quotas (project, max_concurrent_allocations, "
                    " max_concurrent_systems) VALUES ('proj', 100, 100)"
                )
                row_a = await _resource_by_name(seed, "fi-a")
                row_b = await _resource_by_name(seed, "fi-b")
            ids = [row_a["id"], row_b["id"]]
            assert all(isinstance(i, UUID) for i in ids)
            results = []
            for resource_id in ids:
                async with pool.connection() as conn:
                    results.append(
                        await request_admission(
                            conn,
                            RequestContext(
                                principal="alice", agent_session="s", projects=("proj",)
                            ),
                            project="proj",
                            spec=AdmissionRequestSpec(
                                resource_id=cast(UUID, resource_id),
                                kind=ResourceKind.FAULT_INJECT,
                                shape="small",
                                vcpus=None,
                                memory_gb=None,
                                disk_gb=None,
                                window=None,
                                pcie_devices=(),
                                on_capacity="deny",
                            ),
                        )
                    )
        for resource_id, result in zip(ids, results, strict=True):
            assert result.error is None, f"{resource_id}: unexpected error {result.error}"
            assert result.allocation is not None, f"{resource_id}: not admitted: {result.denial}"
        # The two allocations landed on the two distinct resources.
        landed = {str(r.allocation.resource_id) for r in results if r.allocation is not None}
        assert landed == {str(ids[0]), str(ids[1])}

    asyncio.run(_run())


# --- Phase 3 Task 3.2: reconcile_build_hosts -----------------------------------------


def _build_host_toml(
    *,
    name: str = "bh-1",
    kind: str = "local",
    workspace_root: str = "/var/lib/kdive/build",
    max_concurrent: int = 2,
    base_image_volume: str | None = None,
) -> str:
    lines = [
        "schema_version = 2",
        "[[build_host]]",
        f'name = "{name}"',
        f'kind = "{kind}"',
        f'workspace_root = "{workspace_root}"',
        f"max_concurrent = {max_concurrent}",
    ]
    if base_image_volume is not None:
        lines.append(f'base_image_volume = "{base_image_volume}"')
    return "\n".join(lines) + "\n"


async def _build_host_by_name(conn: psycopg.AsyncConnection, name: str) -> dict[str, object]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM build_hosts WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None, f"no build_hosts row named {name!r}"
    return row


async def _build_host_count(conn: psycopg.AsyncConnection, name: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM build_hosts WHERE name = %s", (name,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_build_host_reconciles_to_a_row(migrated_url: str, tmp_path: Path) -> None:
    # Task 3.2: a [[build_host]] reconciles into a build_hosts row carrying its config fields
    # and managed_by='config'.
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                _build_host_toml(name="bh-local", kind="local", max_concurrent=3),
            )
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_hosts(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "bh-local")
        assert row["managed_by"] == "config"
        assert row["kind"] == "local"
        assert row["workspace_root"] == "/var/lib/kdive/build"
        assert row["max_concurrent"] == 3
        assert row["enabled"] is True
        assert "bh-local" in {c.name for c in diff.created}

    asyncio.run(_run())


def test_build_host_ssh_kind_warns_and_skips(migrated_url: str, tmp_path: Path) -> None:
    # The v2 [[build_host]] model carries no address/ssh_credential_ref, so a config-declared
    # 'ssh' host cannot satisfy the build_hosts_fields_check (ssh requires both). Rather than
    # abort the pass on a CHECK violation, reconcile WARNS and skips it (ssh hosts are
    # registered imperatively via build_hosts.register, which carries those fields).
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _build_host_toml(name="bh-ssh", kind="ssh")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_hosts(conn, doc)  # must not raise a CheckViolation
        async with await _connect(migrated_url) as check:
            assert await _build_host_count(check, "bh-ssh") == 0  # not created
        assert any("bh-ssh" in w.name for w in diff.warned)
        assert "bh-ssh" not in {c.name for c in diff.created}

    asyncio.run(_run())


def test_build_host_ephemeral_carries_base_image_volume(migrated_url: str, tmp_path: Path) -> None:
    # Task 3.2: an ephemeral_libvirt build host carries base_image_volume (the field CHECK in
    # 0029 requires it for that kind, and forbids address/ssh_credential_ref).
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                _build_host_toml(
                    name="bh-eph",
                    kind="ephemeral_libvirt",
                    base_image_volume="kdive-base.qcow2",
                ),
            )
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_build_hosts(conn, doc)
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "bh-eph")
        assert row["kind"] == "ephemeral_libvirt"
        assert row["base_image_volume"] == "kdive-base.qcow2"
        assert row["address"] is None
        assert row["ssh_credential_ref"] is None

    asyncio.run(_run())


def test_build_host_adopts_existing_runtime_row_not_duplicated(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.2 adopt-on-collision: the seeded 'worker-local' row from 0027 is managed_by=
    # 'runtime'; declaring it in config ADOPTS it (flip to 'config'), never a second row.
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        doc = load_inventory(
            _write_toml(
                tmp_path,
                _build_host_toml(
                    name="worker-local",
                    kind="local",
                    workspace_root="/var/lib/kdive/build",
                    max_concurrent=1000,
                ),
            )
        )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_build_hosts(conn, doc)
        async with await _connect(migrated_url) as check:
            assert await _build_host_count(check, "worker-local") == 1  # adopted, not duplicated
            row = await _build_host_by_name(check, "worker-local")
        assert row["managed_by"] == "config"  # flipped from runtime
        assert "worker-local" in {u.name for u in diff.updated}

    asyncio.run(_run())


def test_build_host_prune_of_busy_host_cordons_not_deletes(
    migrated_url: str, tmp_path: Path
) -> None:
    # Task 3.2 DB-guarded prune: build_host_leases FKs build_hosts(id) ON DELETE RESTRICT, so a
    # host with an in-flight lease cannot be deleted — prune must CORDON (enabled=false), never
    # attempt a DELETE that would abort the whole pass.
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        # First reconcile a config ssh host, then attach an in-flight lease, then reconcile an
        # empty doc so the host leaves config.
        present = load_inventory(_write_toml(tmp_path, _build_host_toml(name="bh-busy")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_build_hosts(conn, present)
            async with await _connect(migrated_url) as seed:
                busy = await _build_host_by_name(seed, "bh-busy")
                busy_id = busy["id"]
                assert isinstance(busy_id, UUID)
                await seed.execute(
                    "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
                    (uuid4(), busy_id),
                )
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_build_hosts(conn, empty)  # must not raise on RESTRICT
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "bh-busy")  # still present
        assert row["enabled"] is False  # cordoned
        assert "bh-busy" in {c.name for c in diff.cordoned}
        assert "bh-busy" not in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_build_host_prune_of_idle_config_host_deletes(migrated_url: str, tmp_path: Path) -> None:
    # An idle config build host that leaves the file is pruned (row deleted); a runtime host is
    # untouched (prune is config-only).
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        present = load_inventory(_write_toml(tmp_path, _build_host_toml(name="bh-idle")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_build_hosts(conn, present)
            empty = load_inventory(_write_toml(tmp_path, "schema_version = 2\n"))
            async with pool.connection() as conn:
                diff = await reconcile_build_hosts(conn, empty)
        async with await _connect(migrated_url) as check:
            assert await _build_host_count(check, "bh-idle") == 0  # idle config row pruned
            assert await _build_host_count(check, "worker-local") == 1  # seeded runtime untouched
        assert "bh-idle" in {p.name for p in diff.pruned}

    asyncio.run(_run())


def test_build_host_prune_serializes_against_concurrent_lease_no_abort(
    migrated_url: str, tmp_path: Path
) -> None:
    # Load-bearing TOCTOU guard: prune_or_cordon_build_host SELECT ... FOR UPDATE on the
    # build_hosts row, while a concurrent build_host_leases INSERT takes Postgres' implicit
    # FOR KEY SHARE on that same parent row (FK check). FOR UPDATE conflicts with FOR KEY
    # SHARE, so the two serialize: a lease cannot slip in between the liveness check and the
    # DELETE to make the DELETE hit ON DELETE RESTRICT and abort the pass. Here the prune wins
    # the lock first; the concurrent lease INSERT must block, then fail FK after the row is gone
    # (no orphan, no pass abort). This test exercises the lock ordering with raw SQL mirroring
    # prune_or_cordon_build_host's SELECT ... FOR UPDATE → DELETE sequence.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO build_hosts (name, kind, workspace_root, max_concurrent, managed_by) "
                "VALUES ('race-bh', 'local', '/w', 5, 'config')"
            )
            row = await _build_host_by_name(seed, "race-bh")
            hid = row["id"]
            assert isinstance(hid, UUID)

        prune_conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=False)
        lease_conn = await psycopg.AsyncConnection.connect(migrated_url, autocommit=False)
        try:
            # Hold the build_hosts row under FOR UPDATE on a separate connection, then fire the
            # concurrent lease insert and assert it BLOCKS (cannot acquire FOR KEY SHARE).
            async with prune_conn.transaction():
                cur = await prune_conn.execute(
                    "SELECT id FROM build_hosts WHERE id = %s FOR UPDATE", (hid,)
                )
                await cur.fetchone()

                async def _insert_lease() -> None:
                    async with lease_conn.transaction():
                        await lease_conn.execute(
                            "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
                            (uuid4(), hid),
                        )

                task = asyncio.create_task(_insert_lease())
                await asyncio.sleep(0.3)
                assert not task.done()  # blocked behind the prune's FOR UPDATE

                # The prune sees no lease and deletes the row, then commits.
                await prune_conn.execute("DELETE FROM build_hosts WHERE id = %s", (hid,))

            # After the prune commits, the blocked lease insert fails FK (parent row gone) — it
            # never orphans a lease and never aborts the prune.
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                await task
        finally:
            await prune_conn.close()
            await lease_conn.close()

        async with await _connect(migrated_url) as check:
            assert await _build_host_count(check, "race-bh") == 0  # pruned cleanly

    asyncio.run(_run())


def test_build_host_prune_cordons_when_lease_wins_lock_first(
    migrated_url: str, tmp_path: Path
) -> None:
    # The other ordering: a lease lands (committed) before prune runs. prune_or_cordon_build_host
    # then sees the lease and CORDONS (enabled=false), never attempting the RESTRICT-aborting
    # DELETE. This is the same as the busy-host test but via the helper directly, closing the
    # "lease committed first" half of the race.
    from kdive.inventory.reconcile import prune_or_cordon_build_host

    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await seed.execute(
                "INSERT INTO build_hosts (name, kind, workspace_root, max_concurrent, managed_by) "
                "VALUES ('race-bh2', 'local', '/w', 5, 'config')"
            )
            row = await _build_host_by_name(seed, "race-bh2")
            hid = row["id"]
            assert isinstance(hid, UUID)
            await seed.execute(
                "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
                (uuid4(), hid),
            )
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            outcome = await prune_or_cordon_build_host(conn, hid, "race-bh2")  # must not raise
        assert outcome.cordoned is True
        assert outcome.pruned is False
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "race-bh2")
        assert row["enabled"] is False

    asyncio.run(_run())


def test_build_host_reconcile_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    # A second pass over an unchanged doc is a clean no-op (change-detecting upserts).
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        doc = load_inventory(_write_toml(tmp_path, _build_host_toml(name="bh-idem")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_build_hosts(conn, doc)
            async with pool.connection() as conn:
                diff2 = await reconcile_build_hosts(conn, doc)
        assert not diff2.created and not diff2.updated and not diff2.pruned
        assert not diff2.cordoned

    asyncio.run(_run())


def test_loop_inventory_pass_reconciles_a_build_host(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The loop's reconcile_inventory pass also reconciles [[build_host]] into build_hosts (wired
    # after the resource pass). A present, valid file lands the config build-host row.
    async def _run() -> None:
        path = _write_toml(
            tmp_path,
            "schema_version = 2\n"
            "[[build_host]]\n"
            'name = "loop-bh"\n'
            'kind = "local"\n'
            'workspace_root = "/var/lib/kdive/build"\n'
            "max_concurrent = 2\n",
        )
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=_config_with_inventory_spec())
        assert "reconcile_inventory" not in report.failures
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "loop-bh")
        assert row["managed_by"] == "config"
        assert row["max_concurrent"] == 2

    asyncio.run(_run())


# --- Phase 4 Task 4.4: adopt-on-collision (runtime row -> config) --------------------
#
# A config identity (name) matching a managed_by='runtime' row is ADOPTED: managed_by flips
# to 'config', the runtime lease (lease_expires_at) is cleared (config rows carry no lease),
# and the config-declared affinity is taken — the model declares no per-instance scope, so a
# config row is always global (owner_project NULL, empty allowlist), widening a previously
# project-scoped runtime resource. Registration and reconcile serialize on the (kind, name)
# identity (a RESOURCE advisory lock keyed by name) so prune cannot race a re-register.


async def _insert_runtime_fault_inject(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    owner_project: str | None,
    affinity_allowlist: list[str] | None = None,
    vcpus: int = 8,
    memory_mb: int = 16384,
) -> UUID:
    """Insert a leased, project-scoped runtime fault-inject row (the resources.register shape)."""
    from datetime import UTC, datetime, timedelta

    rid = uuid4()
    lease = datetime.now(UTC) + timedelta(hours=1)
    await conn.execute(
        "INSERT INTO resources (id, kind, name, capabilities, pool, cost_class, status, "
        " host_uri, managed_by, owner_project, affinity_allowlist, lease_expires_at) "
        "VALUES (%s, 'fault-inject', %s, %s, 'default', 'local', 'available', "
        " 'fault-inject://local', 'runtime', %s, %s, %s)",
        (
            rid,
            name,
            Jsonb({"vcpus": vcpus, "memory_mb": memory_mb, "concurrent_allocation_cap": 1}),
            owner_project,
            affinity_allowlist or [],
            lease,
        ),
    )
    return rid


def test_adopt_runtime_row_clears_lease_and_widens_to_global(
    migrated_url: str, tmp_path: Path
) -> None:
    # Invariant 6: a config identity matching a project-scoped, leased runtime row ADOPTS it —
    # managed_by flips to config, the lease is cleared, and (no per-instance scope in the file)
    # affinity widens to global (owner_project NULL, empty allowlist).
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            rid = await _insert_runtime_fault_inject(
                seed, name="fi-adopt", owner_project="proj-a", affinity_allowlist=["proj-b"]
            )
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-adopt")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            diff = await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM resources WHERE id = %s", (rid,))
            row = await cur.fetchone()
        assert row is not None  # adopted in place — same row, no duplicate
        assert row["managed_by"] == "config"  # flipped from runtime
        assert row["lease_expires_at"] is None  # lease cleared (config rows carry no lease)
        assert row["owner_project"] is None  # widened to global
        assert list(row["affinity_allowlist"]) == []  # config affinity (default global)
        assert "fi-adopt" in {u.name for u in diff.updated}

    asyncio.run(_run())


def test_adopt_does_not_create_duplicate_row(migrated_url: str, tmp_path: Path) -> None:
    # Adoption updates the existing runtime row in place — exactly one (kind, name) row remains,
    # never a second config row colliding on the partial-unique index.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-dup", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-dup")))
        async with (
            AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool,
            pool.connection() as conn,
        ):
            await reconcile_resources(conn, doc)
        async with await _connect(migrated_url) as check, check.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM resources WHERE kind = 'fault-inject' AND name = 'fi-dup'"
            )
            row = await cur.fetchone()
        assert row is not None
        assert int(row[0]) == 1  # adopted in place, not duplicated

    asyncio.run(_run())


def test_adopted_row_is_not_pruned_when_still_in_config(migrated_url: str, tmp_path: Path) -> None:
    # A re-run over the same config (the adopted row is now managed_by='config') is a clean
    # no-op: the adopted row stays, never re-flagged as a phantom change.
    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-steady", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-steady")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_resources(conn, doc)  # adopt
            async with pool.connection() as conn:
                diff2 = await reconcile_resources(conn, doc)  # steady-state re-run
        assert not diff2.created and not diff2.updated and not diff2.pruned
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-steady")
        assert row["managed_by"] == "config"

    asyncio.run(_run())


def test_reconcile_serializes_with_register_on_the_resource_name(
    migrated_url: str, tmp_path: Path
) -> None:
    # Registration + reconcile serialize on the (kind, name) identity: while a holder holds the
    # RESOURCE advisory lock keyed by the resource name, a concurrent reconcile of that name
    # BLOCKS (cannot race the adopt/prune), then proceeds once the holder releases.
    from kdive.db.locks import LockScope, advisory_xact_lock
    from kdive.domain.models import ResourceKind
    from tests.db_waits import wait_until_any_backend_waiting

    async def _run() -> None:
        async with await _connect(migrated_url) as seed:
            await _insert_runtime_fault_inject(seed, name="fi-race", owner_project="proj-a")
        doc = load_inventory(_write_toml(tmp_path, _fault_inject_toml(name="fi-race")))
        lock_key = f"{ResourceKind.FAULT_INJECT.value}:fi-race"
        async with (
            AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool,
            pool.connection() as holder,
        ):
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.RESOURCE, lock_key),
            ):
                task = asyncio.create_task(_reconcile_on(pool, doc))
                await wait_until_any_backend_waiting(holder, locktype="advisory")
                assert not task.done(), "reconcile did not block on the held resource-name lock"
            diff = await task
        assert "fi-race" in {u.name for u in diff.updated}  # adopted once the lock released
        async with await _connect(migrated_url) as check:
            row = await _resource_by_name(check, "fi-race")
        assert row["managed_by"] == "config"
        assert row["lease_expires_at"] is None

    asyncio.run(_run())


async def _reconcile_on(pool: AsyncConnectionPool, doc: Any) -> ReconcileDiff:
    async with pool.connection() as conn:
        return await reconcile_resources(conn, doc)


def test_build_host_readopt_clears_disabled_cordon(migrated_url: str, tmp_path: Path) -> None:
    # Re-declaring a cordoned (enabled=false) config host in the file re-enables it: the cordon
    # is a prune artifact, so an explicit re-declaration must clear it (idempotent recovery).
    from kdive.inventory.reconcile_build_hosts import reconcile_build_hosts

    async def _run() -> None:
        present = load_inventory(_write_toml(tmp_path, _build_host_toml(name="bh-revive")))
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            async with pool.connection() as conn:
                await reconcile_build_hosts(conn, present)
            async with await _connect(migrated_url) as seed:
                await seed.execute(
                    "UPDATE build_hosts SET enabled = false WHERE name = 'bh-revive'"
                )
            async with pool.connection() as conn:
                diff = await reconcile_build_hosts(conn, present)
        async with await _connect(migrated_url) as check:
            row = await _build_host_by_name(check, "bh-revive")
        assert row["enabled"] is True  # re-enabled by the re-declaration
        assert "bh-revive" in {u.name for u in diff.updated}

    asyncio.run(_run())
