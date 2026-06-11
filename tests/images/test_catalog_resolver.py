"""Resolver cutover: resolve_rootfs returns one registered image visible to a project.

Public-or-owned, private-shadows-public on the same (provider, name); only `registered` rows
resolve (a `defined`-only baseline is listed but not bootable).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.images.catalog import resolve_rootfs

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_FUTURE = datetime.now(UTC) + timedelta(days=365)


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
        "digest": "sha256:abc",
        "capabilities": ["console", "drgn"],
        "provenance": {"releasever": "43"},
        "visibility": ImageVisibility.PUBLIC,
        "owner": None,
        "expires_at": None,
        "state": ImageState.REGISTERED,
    }
    base.update(kw)
    return ImageCatalogEntry.model_validate(base)


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def test_resolves_registered_public(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is not None
            assert result.visibility is ImageVisibility.PUBLIC
            assert result.object_key == "images/local-libvirt/base/x86_64.qcow2"

    asyncio.run(_run())


def test_defined_only_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(
                conn, _entry(state=ImageState.DEFINED, object_key=None, digest=None)
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is None

    asyncio.run(_run())


def test_pending_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(state=ImageState.PENDING))
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is None

    asyncio.run(_run())


def test_unknown_identity_resolves_none(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry())
            assert await resolve_rootfs(conn, "local-libvirt", "other", project="proj") is None
            assert await resolve_rootfs(conn, "other", "base", project="proj") is None

    asyncio.run(_run())


def test_private_shadows_public_for_owning_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(object_key="images/public"))
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj",
                    expires_at=_FUTURE,
                ),
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert result is not None
            assert result.visibility is ImageVisibility.PRIVATE
            assert result.object_key == "images/private"

    asyncio.run(_run())


def test_other_project_sees_public_not_private(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(conn, _entry(object_key="images/public"))
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-a",
                    expires_at=_FUTURE,
                ),
            )
            result = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b")
            assert result is not None
            assert result.visibility is ImageVisibility.PUBLIC
            assert result.object_key == "images/public"

    asyncio.run(_run())


def test_private_only_invisible_to_other_project(migrated_url: str) -> None:
    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await IMAGE_CATALOG.insert(
                conn,
                _entry(
                    object_key="images/private",
                    visibility=ImageVisibility.PRIVATE,
                    owner="proj-a",
                    expires_at=_FUTURE,
                ),
            )
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b") is None

    asyncio.run(_run())
