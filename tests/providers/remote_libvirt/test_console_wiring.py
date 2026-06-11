"""Tests for the remote console part store + assembly wiring (ADR-0095)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import psycopg

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.remote_libvirt.console_wiring import RemoteConsolePartStore


class FakeObjectStore:
    """An in-memory object store satisfying the part store's _StorePort slice."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = request.data
        return StoredArtifact(key, f"etag-{len(self.objects)}", request.sensitivity, "console")

    def get_artifact(self, key: str, etag):  # noqa: ANN001, ANN201
        from kdive.provider_components.artifacts import FetchedArtifact

        return FetchedArtifact(self.objects[key], Sensitivity.REDACTED, "console")

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def test_parts_roundtrip_and_index_listing() -> None:
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    part_store.put_part(sid, 0, b"zero")
    part_store.put_part(sid, 1, b"one")
    part_store.put_part(sid, 10, b"ten")
    assert part_store.list_part_indices(sid) == [0, 1, 10]
    assert part_store.read_part(sid, 1) == b"one"
    part_store.delete_part(sid, 1)
    assert part_store.list_part_indices(sid) == [0, 10]


def test_parts_do_not_shadow_the_console_artifact_prefix() -> None:
    # The single console artifact key must not be picked up as a numbered part.
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    store.objects[f"remote-libvirt/systems/{sid}/console"] = b"assembled"
    part_store.put_part(sid, 0, b"part0")
    assert part_store.list_part_indices(sid) == [0]


def test_write_console_artifact_registers_row(migrated_url: str) -> None:
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, migrated_url)
    sid = uuid4()
    part_store.write_console_artifact(sid, b"boot ... crash")
    key = f"remote-libvirt/systems/{sid}/console"
    assert store.objects[key] == b"boot ... crash"

    async def _check() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            cur = await conn.execute(
                "SELECT owner_kind, sensitivity, retention_class FROM artifacts "
                "WHERE owner_id = %s AND object_key = %s",
                (sid, key),
            )
            row = await cur.fetchone()
        assert row == ("systems", "redacted", "console")

    asyncio.run(_check())


def test_write_console_artifact_refreshes_etag_on_reassembly(migrated_url: str) -> None:
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, migrated_url)
    sid = uuid4()
    part_store.write_console_artifact(sid, b"first")
    part_store.write_console_artifact(sid, b"second")  # re-finalize updates the same row
    key = f"remote-libvirt/systems/{sid}/console"

    async def _check() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM artifacts WHERE owner_id = %s AND object_key = %s",
                (sid, key),
            )
            row = await cur.fetchone()
        assert row is not None and row[0] == 1  # one row, etag refreshed not duplicated

    asyncio.run(_check())
