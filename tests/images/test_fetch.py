"""Object-store fetch for a registered catalog rootfs: download to a checksum-verified cache.

This wires the `not wired yet` materialization stub: resolve the registered row, download its
`object_key`, verify the SHA-256 against the row's `digest`, and cache it locally by digest.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility, Sensitivity
from kdive.images.fetch import fetch_registered_rootfs
from kdive.provider_components import artifacts as artifact_types

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_QCOW2 = b"qcow2-bytes-for-test"
_DIGEST = "sha256:" + hashlib.sha256(_QCOW2).hexdigest()


class _FakeStore:
    """A minimal ObjectStore stand-in returning fixed bytes for one key."""

    def __init__(self, key: str, data: bytes) -> None:
        self._key = key
        self._data = data
        self.gets: list[str] = []

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
        self.gets.append(key)
        if key != self._key:
            raise CategorizedError("missing", category=ErrorCategory.STALE_HANDLE)
        return artifact_types.FetchedArtifact(self._data, Sensitivity.REDACTED, "image")


def _entry(**kw: object) -> ImageCatalogEntry:
    base: dict[str, object] = {
        "id": uuid4(),
        "created_at": _DT,
        "updated_at": _DT,
        "pending_since": _DT,
        "provider": "local-libvirt",
        "name": "base",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "object_key": "images/local-libvirt/base/x86_64.qcow2",
        "digest": _DIGEST,
        "capabilities": ["console"],
        "provenance": {},
        "visibility": ImageVisibility.PUBLIC,
        "owner": None,
        "expires_at": None,
        "state": ImageState.REGISTERED,
    }
    base.update(kw)
    return ImageCatalogEntry.model_validate(base)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_fetch_downloads_and_caches_by_digest(migrated_url: str, tmp_path: Path) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            path = await fetch_registered_rootfs(
                conn,
                store,
                provider="local-libvirt",
                name="base",
                project="proj",
                cache_dir=tmp_path,
            )
            assert path.read_bytes() == _QCOW2
            assert path.parent == tmp_path

    asyncio.run(_run())


def test_fetch_reuses_cached_object(migrated_url: str, tmp_path: Path) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            first = await fetch_registered_rootfs(
                conn,
                store,
                provider="local-libvirt",
                name="base",
                project="proj",
                cache_dir=tmp_path,
            )
            second = await fetch_registered_rootfs(
                conn,
                store,
                provider="local-libvirt",
                name="base",
                project="proj",
                cache_dir=tmp_path,
            )
            assert first == second
            # A cache hit does not re-download.
            assert len(store.gets) == 1

    asyncio.run(_run())


def test_fetch_unknown_identity_raises_config_error(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore("images/x", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await fetch_registered_rootfs(
                    conn,
                    store,
                    provider="local-libvirt",
                    name="missing",
                    project="proj",
                    cache_dir=tmp_path,
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())


def test_fetch_rejects_malformed_digest(migrated_url: str, tmp_path: Path) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    # A row whose digest is not sha256:<hex> must not form a cache path (traversal guard).
    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(digest="sha256:../../../etc/passwd"))
            with pytest.raises(CategorizedError) as err:
                await fetch_registered_rootfs(
                    conn,
                    store,
                    provider="local-libvirt",
                    name="base",
                    project="proj",
                    cache_dir=tmp_path,
                )
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert store.gets == []  # rejected before any download

    asyncio.run(_run())


def test_fetch_checksum_mismatch_raises_infra(migrated_url: str, tmp_path: Path) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    # The stored object's bytes disagree with the row's digest — a corrupted/tampered object.
    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", b"different-bytes")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            with pytest.raises(CategorizedError) as err:
                await fetch_registered_rootfs(
                    conn,
                    store,
                    provider="local-libvirt",
                    name="base",
                    project="proj",
                    cache_dir=tmp_path,
                )
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            # The corrupt download is not left in the cache.
            assert list(tmp_path.iterdir()) == []

    asyncio.run(_run())


def test_fetch_cache_mkdir_error_is_typed(migrated_url: str, tmp_path: Path) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    cache_dir = tmp_path / "cache"
    cache_dir.write_bytes(b"not-a-directory")
    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            with pytest.raises(CategorizedError) as err:
                await fetch_registered_rootfs(
                    conn,
                    store,
                    provider="local-libvirt",
                    name="base",
                    project="proj",
                    cache_dir=cache_dir,
                )

            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert err.value.details == {
                "provider": "local-libvirt",
                "name": "base",
                "object_key": "images/local-libvirt/base/x86_64.qcow2",
                "cache_path": str(cache_dir / f"{_DIGEST.removeprefix('sha256:')}.qcow2"),
            }
            assert isinstance(err.value.__cause__, OSError)
            assert store.gets == []

    asyncio.run(_run())


def test_fetch_cache_replace_error_is_typed_and_removes_partial(
    migrated_url: str, tmp_path: Path
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    cached = tmp_path / f"{_DIGEST.removeprefix('sha256:')}.qcow2"
    cached.mkdir()
    partial = cached.with_suffix(".qcow2.partial")
    store = _FakeStore("images/local-libvirt/base/x86_64.qcow2", _QCOW2)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            with pytest.raises(CategorizedError) as err:
                await fetch_registered_rootfs(
                    conn,
                    store,
                    provider="local-libvirt",
                    name="base",
                    project="proj",
                    cache_dir=tmp_path,
                )

            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert err.value.details["provider"] == "local-libvirt"
            assert err.value.details["name"] == "base"
            assert err.value.details["object_key"] == "images/local-libvirt/base/x86_64.qcow2"
            assert err.value.details["cache_path"] == str(cached)
            assert isinstance(err.value.__cause__, OSError)
            assert not partial.exists()

    asyncio.run(_run())
