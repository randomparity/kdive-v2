"""Project-private upload registration (ADR-0093, issue #286).

``register_private_upload`` runs under the project advisory lock: it enforces the per-project
count/bytes quota fail-closed, validates the quarantined object's guest contract, then delegates
to ``publish_image`` with ``visibility='private'``/``owner=project``. These tests pin: a
non-conforming image is rejected with a named reason while still quarantined (never registered);
an over-cap upload is denied fail-closed and audited; two concurrent uploads cannot both pass the
cap (held PROJECT lock); a registered private image resolves only within its owning project and
shadows a same-identity public image there.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_BYTES,
    IMAGE_PRIVATE_MAX_COUNT,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageState, ImageVisibility, Sensitivity
from kdive.images.catalog import resolve_rootfs
from kdive.images.validation import GUEST_CONTRACT_PATHS, InspectSeam
from kdive.provider_components import artifacts as artifact_types
from kdive.services.images.upload import register_private_upload

_REQUIRED = ("agent", "kdump", "drgn", "helpers")
_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _conforming() -> InspectSeam:
    """An inspection seam reporting every guest-contract path as present."""

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return set(candidates)

    return _probe


def _missing(*absent: str) -> InspectSeam:
    """An inspection seam where the named contract elements are absent."""
    absent_paths = {GUEST_CONTRACT_PATHS[a] for a in absent}

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return {c for c in candidates if c not in absent_paths}

    return _probe


class _FakeStore:
    """In-memory store: get_artifact serves a seeded quarantined object; put/head mirror writes."""

    def __init__(self, quarantined: dict[str, bytes] | None = None) -> None:
        self._objects: dict[str, bytes] = dict(quarantined or {})
        self.puts: list[str] = []

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
        data = self._objects.get(key)
        if data is None:
            raise CategorizedError(
                f"artifact {key!r} is gone",
                category=ErrorCategory.STALE_HANDLE,
                details={"key": key},
            )
        return artifact_types.FetchedArtifact(data, Sensitivity.QUARANTINED, "upload")

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        key = request.key()
        self.puts.append(key)
        self._objects[key] = request.data
        etag = hashlib.md5(request.data).hexdigest()  # noqa: S324 - etag stand-in, not security
        return artifact_types.StoredArtifact(
            key, etag, request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(size_bytes=len(data), checksum_sha256=None, etag="etag")


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _quarantine(payload: bytes, key: str = "uploads/q/proj/rootfs.qcow2") -> _FakeStore:
    return _FakeStore({key: payload})


async def _register(
    conn: psycopg.AsyncConnection,
    store: _FakeStore,
    *,
    project: str = "proj",
    principal: str = "alice",
    name: str = "myrootfs",
    quarantine_key: str = "uploads/q/proj/rootfs.qcow2",
    expires_at: datetime | None = None,
    inspect: InspectSeam | None = None,
):
    return await register_private_upload(
        conn,
        store,
        project=project,
        principal=principal,
        name=name,
        provider="local-libvirt",
        arch="x86_64",
        quarantine_key=quarantine_key,
        expires_at=expires_at or (_DT + timedelta(days=3)),
        required=_REQUIRED,
        inspect=inspect or _conforming(),
    )


_UPLOAD_TOOL = "images.upload"


async def _denial_rows(conn: psycopg.AsyncConnection) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM audit_log WHERE tool = %s AND transition = 'denied'",
            (_UPLOAD_TOOL,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_registers_private_image_resolving_only_within_owning_project(migrated_url: str) -> None:
    store = _quarantine(b"conforming-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await _register(conn, store)
            assert entry.state is ImageState.REGISTERED
            assert entry.visibility is ImageVisibility.PRIVATE
            assert entry.owner == "proj"
            assert entry.object_key is not None
            # Resolves for the owning project, not for another.
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="proj")
            assert mine is not None and mine.id == entry.id
            assert await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other") is None

    asyncio.run(_run())


def test_private_shadows_public_on_same_provider_name(migrated_url: str) -> None:
    from kdive.services.images.publish import PublishRequest, publish_image

    payload = b"private-rootfs"
    store = _quarantine(payload)

    async def _run(tmp: Path) -> None:
        async with await _connect(migrated_url) as conn:
            pub_src = tmp / "pub.qcow2"
            pub_src.write_bytes(b"public-rootfs")
            await publish_image(
                conn,
                store,
                request=PublishRequest(
                    provider="local-libvirt",
                    name="myrootfs",
                    arch="x86_64",
                    format="qcow2",
                    root_device="/dev/vda",
                    digest="sha256:" + hashlib.sha256(b"public-rootfs").hexdigest(),
                    capabilities=(),
                    provenance={},
                    visibility="public",
                ),
                source=pub_src,
            )
            private = await _register(conn, store)
            # The owning project gets its private image; another project gets the public one.
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="proj")
            other = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other")
            assert mine is not None and mine.id == private.id
            assert other is not None and other.visibility is ImageVisibility.PUBLIC

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(_run(Path(d)))


def test_non_conforming_image_rejected_while_quarantined(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    store = _quarantine(b"missing-agent-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, inspect=_missing("agent"))
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert "agent" in str(err.value)
            assert err.value.details.get("missing") == "agent"
            # Never registered: no catalog row, the object never left quarantine (no put).
            assert await IMAGE_CATALOG.list_all(conn) == []
            assert store.puts == []

    asyncio.run(_run())


def test_over_count_cap_denied_fail_closed_and_audited(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")
    store = _quarantine(b"rootfs-a")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _register(conn, store, name="first")
            denied_before = await _denial_rows(conn)
            store._objects["uploads/q/proj/b.qcow2"] = b"rootfs-b"  # noqa: SLF001 - test seam
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            # Fail-closed: the second image is not registered, and the denial is audited.
            registered = [r for r in await IMAGE_CATALOG.list_all(conn) if r.name == "second"]
            assert registered == []
            assert await _denial_rows(conn) == denied_before + 1

    asyncio.run(_run())


def test_over_bytes_cap_denied_fail_closed(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "10")
    store = _quarantine(b"this-is-more-than-ten-bytes")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store)
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            assert store.puts == []

    asyncio.run(_run())


def test_accumulated_bytes_cap_denied_under_lock(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither image alone exceeds the cap, but the second pushes the project total over it. The
    # under-lock authoritative check (current usage + new bytes) must deny — not just the
    # single-object pre-check.
    monkeypatch.setenv(IMAGE_PRIVATE_MAX_BYTES.name, "20")
    store = _quarantine(b"twelve-bytes", key="uploads/q/proj/a.qcow2")  # 12 bytes
    store._objects["uploads/q/proj/b.qcow2"] = b"twelve-bytes"  # noqa: SLF001 - test seam

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            await _register(conn, store, name="first", quarantine_key="uploads/q/proj/a.qcow2")
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED

    asyncio.run(_run())


def test_concurrent_uploads_cannot_both_pass_the_cap(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")

    async def _run() -> None:
        store_a = _quarantine(b"rootfs-aaaa", key="uploads/q/proj/a.qcow2")
        store_b = _quarantine(b"rootfs-bbbb", key="uploads/q/proj/b.qcow2")
        # Share one object namespace so each sees the other's registered image.
        store_b._objects.update(store_a._objects)  # noqa: SLF001 - test seam
        store_a._objects.update(store_b._objects)  # noqa: SLF001 - test seam

        async def _one(store: _FakeStore, name: str, key: str) -> object:
            conn = await _connect(migrated_url)
            try:
                return await _register(conn, store, name=name, quarantine_key=key)
            except CategorizedError as exc:
                return exc
            finally:
                await conn.close()

        results = await asyncio.gather(
            _one(store_a, "alpha", "uploads/q/proj/a.qcow2"),
            _one(store_b, "beta", "uploads/q/proj/b.qcow2"),
        )
        denials = [r for r in results if isinstance(r, CategorizedError)]
        assert len(denials) == 1
        assert denials[0].category is ErrorCategory.QUOTA_EXCEEDED

        async with await _connect(migrated_url) as conn:
            registered = [
                r for r in await IMAGE_CATALOG.list_all(conn) if r.state is ImageState.REGISTERED
            ]
            assert len(registered) == 1

    asyncio.run(_run())


def test_expiry_clamped_to_lifetime_max(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(IMAGE_PRIVATE_LIFETIME_MAX.name, str(3600))
    store = _quarantine(b"rootfs-x")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            far = datetime.now(UTC) + timedelta(days=365)
            entry = await _register(conn, store, expires_at=far)
            assert entry.expires_at is not None
            # Clamped to roughly now + 1h, well below the requested year.
            assert entry.expires_at < datetime.now(UTC) + timedelta(hours=2)

    asyncio.run(_run())


def test_records_principal_in_audit_owner_is_project(
    migrated_url: str,
) -> None:
    store = _quarantine(b"audited-rootfs")

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await _register(conn, store, principal="bob", project="proj")
            assert entry.owner == "proj"
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT principal, project FROM audit_log "
                    "WHERE transition = %s ORDER BY ts DESC LIMIT 1",
                    ("private-upload:registered",),
                )
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "bob"
            assert row[1] == "proj"

    asyncio.run(_run())
