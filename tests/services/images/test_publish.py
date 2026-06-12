"""Row-first publish/register two-write (ADR-0092, issue #285).

The service writes the ``pending`` row before the object, HEAD-gates, then flips to
``registered``. These tests pin: the success path (a ``registered`` row whose object HEADs
and resolves), crash-after-pending-before-object adoptability (no unique-violation wedge),
idempotent re-run (adopt the in-flight ``pending`` row, re-arm ``pending_since``), and realizing
a seeded ``defined`` baseline through the same path.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from kdive.db.repositories import IMAGE_CATALOG
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.images.catalog import resolve_rootfs
from kdive.provider_components import artifacts as artifact_types
from kdive.services.images.publish import PublishRequest, publish_image

_QCOW2 = b"qcow2-bytes-for-publish-test"
_DIGEST = "sha256:" + hashlib.sha256(_QCOW2).hexdigest()
_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeStore:
    """An in-memory ObjectStore stand-in: put records bytes, head reflects them."""

    def __init__(self, *, fail_put: bool = False, drop_object: bool = False) -> None:
        self._objects: dict[str, bytes] = {}
        self._fail_put = fail_put
        self._drop_object = drop_object
        self.puts: list[str] = []
        self.heads: list[str] = []

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        key = request.key()
        self.puts.append(key)
        if self._fail_put:
            raise CategorizedError(
                "object store unreachable",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            )
        if not self._drop_object:
            self._objects[key] = request.data
        etag = hashlib.md5(request.data).hexdigest()  # noqa: S324 - etag stand-in, not security
        return artifact_types.StoredArtifact(
            key, etag, request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        self.heads.append(key)
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(size_bytes=len(data), checksum_sha256=None, etag="etag")


def _request(
    *,
    visibility: ImageVisibility = ImageVisibility.PUBLIC,
    owner: str | None = None,
    expires_at: datetime | None = None,
    digest: str = _DIGEST,
) -> PublishRequest:
    return PublishRequest(
        provider="local-libvirt",
        name="base",
        arch="x86_64",
        format="qcow2",
        root_device="/dev/vda",
        digest=digest,
        capabilities=("console", "kdump"),
        provenance={"releasever": "43"},
        visibility=visibility,
        owner=owner,
        expires_at=expires_at,
    )


async def _connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


def _qcow2_source(tmp_path: Path) -> Path:
    src = tmp_path / "rootfs.qcow2"
    src.write_bytes(_QCOW2)
    return src


def test_publish_request_rejects_scope_fields_that_do_not_match_visibility() -> None:
    with pytest.raises(ValueError, match="owner must be set iff visibility is private"):
        _request(visibility=ImageVisibility.PRIVATE, expires_at=_DT)

    with pytest.raises(ValueError, match="expires_at must be set iff visibility is private"):
        _request(visibility=ImageVisibility.PRIVATE, owner="proj")

    with pytest.raises(ValueError, match="owner must be set iff visibility is private"):
        _request(owner="proj")


def test_publish_leaves_registered_row_that_heads_and_resolves(
    migrated_url: str, tmp_path: Path
) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(conn, store, request=_request(), source=source)
            assert entry.state is ImageState.REGISTERED
            assert entry.object_key is not None
            assert store.head(entry.object_key) is not None
            resolved = await resolve_rootfs(conn, "local-libvirt", "base", project="proj")
            assert resolved is not None
            assert resolved.id == entry.id

    asyncio.run(_run())


def test_crash_after_pending_before_object_leaves_adoptable_state(
    migrated_url: str, tmp_path: Path
) -> None:
    # A store whose put fails models a crash after the pending row, before the object lands.
    failing = _FakeStore(fail_put=True)
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError):
                await publish_image(conn, failing, request=_request(), source=source)
            # The pending row survives, with an object_key set but no object behind it.
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len(rows) == 1
            assert rows[0].state is ImageState.PENDING
            assert rows[0].object_key is not None
            assert failing.head(rows[0].object_key) is None

            # A re-run adopts the pending row (no unique-violation wedge) and registers it.
            healthy = _FakeStore()
            entry = await publish_image(conn, healthy, request=_request(), source=source)
            assert entry.id == rows[0].id
            assert entry.state is ImageState.REGISTERED
            assert (await IMAGE_CATALOG.list_all(conn)) == [
                r for r in await IMAGE_CATALOG.list_all(conn) if r.id == entry.id
            ]

    asyncio.run(_run())


def test_rerun_adopts_pending_and_rearms_pending_since(migrated_url: str, tmp_path: Path) -> None:
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failing = _FakeStore(fail_put=True)
            with pytest.raises(CategorizedError):
                await publish_image(conn, failing, request=_request(), source=source)
            pending = (await IMAGE_CATALOG.list_all(conn))[0]
            original_since = pending.pending_since

            # Age the pending_since so a re-arm is observable.
            await conn.execute(
                "UPDATE image_catalog SET pending_since = %s WHERE id = %s",
                (original_since - timedelta(hours=2), pending.id),
            )

            healthy = _FakeStore()
            entry = await publish_image(conn, healthy, request=_request(), source=source)
            assert entry.id == pending.id
            assert entry.pending_since > original_since - timedelta(hours=2)

    asyncio.run(_run())


def test_realizing_defined_baseline_follows_same_path(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            seeded = ImageCatalogEntry(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                pending_since=_DT,
                provider="local-libvirt",
                name="base",
                arch="x86_64",
                format="qcow2",
                root_device="/dev/vda",
                object_key=None,
                digest=None,
                capabilities=["console"],
                provenance={},
                visibility=ImageVisibility.PUBLIC,
                owner=None,
                expires_at=None,
                state=ImageState.DEFINED,
            )
            inserted = await IMAGE_CATALOG.insert(conn, seeded)

            entry = await publish_image(conn, store, request=_request(), source=source)
            # The seeded defined row is realized in place (defined -> pending -> registered).
            assert entry.id == inserted.id
            assert entry.state is ImageState.REGISTERED
            assert len(await IMAGE_CATALOG.list_all(conn)) == 1

    asyncio.run(_run())


def test_publish_fails_when_object_does_not_head(migrated_url: str, tmp_path: Path) -> None:
    # The put "succeeds" but the object is not actually present: the HEAD gate must catch it
    # and the row stays pending (no false registered).
    store = _FakeStore(drop_object=True)
    source = _qcow2_source(tmp_path)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await publish_image(conn, store, request=_request(), source=source)
            assert err.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            row = (await IMAGE_CATALOG.list_all(conn))[0]
            assert row.state is ImageState.PENDING

    asyncio.run(_run())


def test_publish_rejects_source_digest_mismatch(migrated_url: str, tmp_path: Path) -> None:
    # The declared digest disagrees with the source bytes: publish must fail-fast (a registered
    # row with a mismatched digest would be permanently unfetchable), leaving an adoptable pending.
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    wrong_digest = "sha256:" + "f" * 64

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await publish_image(
                    conn, store, request=_request(digest=wrong_digest), source=source
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len(rows) == 1
            assert rows[0].state is ImageState.PENDING
            assert store.puts == []  # rejected before any object write

    asyncio.run(_run())


def test_two_owners_same_identity_do_not_collide(migrated_url: str, tmp_path: Path) -> None:
    # Two projects publish a private image of the same (provider, name, arch). They must NOT adopt
    # each other's row and must NOT share one object key — cross-tenant isolation.
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            a = await publish_image(
                conn,
                store,
                request=_request(
                    visibility=ImageVisibility.PRIVATE, owner="proj-a", expires_at=expires
                ),
                source=source,
            )
            b = await publish_image(
                conn,
                store,
                request=_request(
                    visibility=ImageVisibility.PRIVATE, owner="proj-b", expires_at=expires
                ),
                source=source,
            )
            assert a.id != b.id
            assert a.object_key != b.object_key
            assert a.owner == "proj-a"
            assert b.owner == "proj-b"
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len([r for r in rows if r.state is ImageState.REGISTERED]) == 2
            # Each owner resolves only its own private image.
            resolved_a = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-a")
            resolved_b = await resolve_rootfs(conn, "local-libvirt", "base", project="proj-b")
            assert resolved_a is not None and resolved_a.id == a.id
            assert resolved_b is not None and resolved_b.id == b.id

    asyncio.run(_run())


def test_public_publish_does_not_adopt_a_private_pending(migrated_url: str, tmp_path: Path) -> None:
    # A crashed private pending row for an identity must not be adopted by a public publish of the
    # same (provider, name, arch) — the match is scoped by visibility/owner.
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            failing = _FakeStore(fail_put=True)
            with pytest.raises(CategorizedError):
                await publish_image(
                    conn,
                    failing,
                    request=_request(
                        visibility=ImageVisibility.PRIVATE, owner="proj-a", expires_at=expires
                    ),
                    source=source,
                )
            private_pending = (await IMAGE_CATALOG.list_all(conn))[0]

            healthy = _FakeStore()
            public = await publish_image(conn, healthy, request=_request(), source=source)
            assert public.id != private_pending.id
            assert public.visibility is ImageVisibility.PUBLIC
            # The private pending row is untouched (still pending, still owned by proj-a).
            still = await IMAGE_CATALOG.get(conn, private_pending.id)
            assert still is not None
            assert still.state is ImageState.PENDING
            assert still.owner == "proj-a"

    asyncio.run(_run())


def test_private_publish_records_owner_and_expiry(migrated_url: str, tmp_path: Path) -> None:
    store = _FakeStore()
    source = _qcow2_source(tmp_path)
    expires = _DT + timedelta(days=7)

    async def _run() -> None:
        async with await _connect(migrated_url) as conn:
            entry = await publish_image(
                conn,
                store,
                request=_request(
                    visibility=ImageVisibility.PRIVATE, owner="proj", expires_at=expires
                ),
                source=source,
            )
            assert entry.visibility is ImageVisibility.PRIVATE
            assert entry.owner == "proj"
            assert entry.expires_at == expires
            # A private image resolves for its owner, not for another project.
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="proj") is not None
            assert await resolve_rootfs(conn, "local-libvirt", "base", project="other") is None

    asyncio.run(_run())
