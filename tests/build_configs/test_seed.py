"""Unit tests for the build-config seed (ADR-0096)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import cast

from psycopg import AsyncConnection

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH, seed_build_configs
from kdive.store.objectstore import ObjectStore


def test_kdump_fragment_is_packaged_and_nonempty() -> None:
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert data.strip()
    assert b"CONFIG_CRASH_DUMP=y" in data


def test_seed_publishes_fragment_and_upserts_row(fake_conn, fake_store) -> None:
    async def _run() -> None:
        published = await seed_build_configs(
            cast(AsyncConnection, fake_conn), cast(ObjectStore, fake_store)
        )
        assert published == 1
        expected_sha = hashlib.sha256(KDUMP_FRAGMENT_PATH.read_bytes()).hexdigest()
        row = fake_conn.upserted_rows["kdump"]
        assert row["sha256"] == expected_sha
        assert row["object_key"] == "system/build-configs/kdump/kdump.config"
        assert fake_store.put_keys == ["system/build-configs/kdump/kdump.config"]

    asyncio.run(_run())


def test_seed_is_idempotent_when_sha_unchanged(fake_conn, fake_store) -> None:
    fake_conn.existing_sha["kdump"] = hashlib.sha256(KDUMP_FRAGMENT_PATH.read_bytes()).hexdigest()

    async def _run() -> None:
        published = await seed_build_configs(
            cast(AsyncConnection, fake_conn), cast(ObjectStore, fake_store)
        )
        assert published == 0
        assert fake_store.put_keys == []  # no re-put when sha matches

    asyncio.run(_run())


def test_seed_overwrites_in_place_on_changed_bytes(fake_conn, fake_store) -> None:
    fake_conn.existing_sha["kdump"] = "stale-sha"

    async def _run() -> None:
        await seed_build_configs(cast(AsyncConnection, fake_conn), cast(ObjectStore, fake_store))
        # fixed reserved key -> same key overwritten, no orphan
        assert fake_store.put_keys == ["system/build-configs/kdump/kdump.config"]

    asyncio.run(_run())
