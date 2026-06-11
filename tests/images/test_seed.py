"""App-level seed: register baseline rootfs as `defined` rows, read-only against the source.

The seed registers metadata (object_key NULL) so a fresh install lists the baseline before any
image is built; `images build`/`publish` realizes a `defined` row to `registered`. It is
idempotent (skips an identity already present) and never deletes the files it read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
import pytest

from kdive.domain.models import ImageState, ImageVisibility
from kdive.images.seed import seed_defined_rootfs

_MANIFEST = (
    "schema_version: 1\n"
    "provider: local-libvirt\n"
    "storage:\n"
    "  allowed_component_roots: [/var/lib/kdive/rootfs]\n"
    "  cache_dir: /var/lib/kdive/rootfs/cache\n"
    "  overlay_dir: /var/lib/kdive/rootfs/overlays\n"
    "rootfs: [rootfs/base.yaml]\n"
    "profiles: []\n"
)
_ROOTFS_YAML = (
    "provider: local-libvirt\n"
    "name: base\n"
    "arch: x86_64\n"
    "format: qcow2\n"
    "root_device: /dev/vda\n"
    "source:\n"
    "  kind: local\n"
    "  path: /var/lib/kdive/rootfs/local/base.qcow2\n"
    "visibility: public\n"
    "capabilities: [console, drgn]\n"
)


def _write_catalog(root: Path) -> None:
    (root / "rootfs").mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    (root / "rootfs" / "base.yaml").write_text(_ROOTFS_YAML, encoding="utf-8")


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_seed_registers_defined_rows(migrated_url: str, tmp_path: Path) -> None:
    catalog = tmp_path / "local-libvirt"
    _write_catalog(catalog)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            count = await seed_defined_rootfs(conn, catalog)
            assert count == 1
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT provider, name, arch, format, root_device, object_key, "
                    "digest, capabilities, visibility, state FROM image_catalog"
                )
                row = await cur.fetchone()
            assert row is not None
            (provider, name, arch, fmt, root_device, object_key, digest, caps, vis, state) = row
            assert (provider, name, arch) == ("local-libvirt", "base", "x86_64")
            assert (fmt, root_device) == ("qcow2", "/dev/vda")
            assert object_key is None and digest is None
            assert set(caps) == {"console", "drgn"}
            assert vis == ImageVisibility.PUBLIC.value
            assert state == ImageState.DEFINED.value

    asyncio.run(_run())


def test_seed_is_idempotent(migrated_url: str, tmp_path: Path) -> None:
    catalog = tmp_path / "local-libvirt"
    _write_catalog(catalog)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            first = await seed_defined_rootfs(conn, catalog)
            second = await seed_defined_rootfs(conn, catalog)
            assert first == 1
            assert second == 0
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM image_catalog")
                row = await cur.fetchone()
            assert row is not None and row[0] == 1

    asyncio.run(_run())


def test_seed_leaves_source_untouched(migrated_url: str, tmp_path: Path) -> None:
    catalog = tmp_path / "local-libvirt"
    _write_catalog(catalog)
    before = {p: p.read_bytes() for p in sorted(catalog.rglob("*")) if p.is_file()}

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await seed_defined_rootfs(conn, catalog)

    asyncio.run(_run())
    after = {p: p.read_bytes() for p in sorted(catalog.rglob("*")) if p.is_file()}
    assert after == before


def test_seed_honors_fixture_catalog_path_override(
    migrated_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "operator-catalog"
    _write_catalog(catalog)
    monkeypatch.setenv("KDIVE_FIXTURE_CATALOG_PATH", str(catalog))

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            count = await seed_defined_rootfs(conn)
            assert count == 1

    asyncio.run(_run())


def test_seed_uses_packaged_baseline_by_default(migrated_url: str) -> None:
    # With no override, the seed reads the packaged baseline relocated into seed_data/ and
    # registers its rootfs entries; the live-stack baseline ships with this package.
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            count = await seed_defined_rootfs(conn)
            assert count >= 1
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM image_catalog WHERE state = %s",
                    (ImageState.DEFINED.value,),
                )
                row = await cur.fetchone()
            assert row is not None and row[0] == count

    asyncio.run(_run())
