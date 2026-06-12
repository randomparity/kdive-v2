"""The M2.4 milestone exit-criterion proof (issue #289; mirrors the M2.3 doctor proof).

One focused test per spec exit criterion (``docs/superpowers/specs/2026-06-10-m24-image-rootfs-
lifecycle-design.md`` §"Exit criteria"), each driven through the **real merged** code — the
real publish/upload services, the real reconciler sweeps, the real async catalog resolver — over
the disposable-Postgres fixture. Only the leaf seams a CI host cannot run carry a fake: the
object store (no MinIO) and the libguestfs guest-contract ``inspect`` probe (no guestfish),
exactly as the doctor proof fakes only the TLS/ACL/egress leaf probes.

The criterion→test mapping:

* **Criterion 1** (a no-op kernel patch fails patch-applied verification, both kernel build
  planes) is proven adjacent to each plane's ``_apply_patch`` in
  ``tests/providers/{local,remote}_libvirt/test_build.py`` (the real ``git apply`` over a
  ``.git``-less workspace), so it is not duplicated here; a guard test below pins that the two
  regressions exist and stay co-located with their plane.
* **Criterion 2** — :func:`test_half_published_object_without_row_is_reconciled` /
  :func:`test_half_published_row_without_object_is_reconciled` (inject each half-state, sweep).
* **Criterion 3** — :func:`test_private_upload_resolves_only_within_owning_project` (isolation),
  :func:`test_expired_private_image_is_auto_pruned`,
  :func:`test_expired_private_referenced_by_live_system_is_not_pruned` (reference guard).
* **Criterion 4** — :func:`test_non_conforming_upload_is_rejected_with_named_reason`,
  :func:`test_over_quota_upload_is_denied` (both audited).
* **Criterion 5** — the local-libvirt rootfs build through the Python plane on the operator-run
  live-stack path is env-gated (``KDIVE_LIVE_SSH_TARGET``), so it is a runbook step, not a CI
  check. :func:`test_exit_criterion_proof_is_ci_tier` pins that this file carries no live marker,
  and ``docs/runbooks/image-lifecycle.md`` records the operator-run criterion-5 evidence.

Why this is not tautological: every criterion runs its production path over a broken/edge input
(a half-published state injected straight into the catalog + object store; an expired row; a
non-conforming image; an over-cap upload), not a hand-built result. The reconciler counts, the
resolver visibility, and the audit rows are read back from the real implementation, so the proof
cannot drift from the shipped behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageVisibility, Sensitivity
from kdive.images.catalog import resolve_rootfs
from kdive.images.validation import GUEST_CONTRACT_PATHS, InspectSeam
from kdive.provider_components import artifacts as artifact_types
from kdive.provider_components.artifacts import ObjectListing
from kdive.reconciler.images import (
    repair_dangling_images,
    repair_expired_private_images,
    repair_leaked_images,
)
from kdive.services.images.publish import PublishRequest, publish_image
from kdive.services.images.upload import register_private_upload
from tests.reconciler.conftest import connect, run_repair, seed_system

_REQUIRED = ("agent", "kdump", "drgn", "helpers")
_GRACE = timedelta(hours=1)
_PROJECT = "proj"
_PRINCIPAL = "alice"
_UPLOAD_TOOL = "images.upload"


# ---- leaf seams: the object store (no MinIO) + the libguestfs inspect (no guestfish) ----


class _FakeImageStore:
    """In-memory image store satisfying both the publish and the sweep ports.

    ``objects`` maps key -> (bytes, age): ``head``/``get_artifact`` serve the bytes,
    ``list_image_objects`` reports each key with a Postgres-relative ``ObjectListing`` so the
    leaked-grace compare stays on the DB clock, ``head_present``/``delete`` back the sweeps.
    """

    def __init__(self, objects: dict[str, tuple[bytes, timedelta]] | None = None) -> None:
        self._objects: dict[str, tuple[bytes, timedelta]] = dict(objects or {})
        self.puts: list[str] = []
        self.deleted: list[str] = []

    # --- publish/upload port ---
    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        key = request.key()
        self.puts.append(key)
        self._objects[key] = (request.data, timedelta())
        etag = hashlib.md5(request.data).hexdigest()  # noqa: S324 - etag stand-in, not security
        return artifact_types.StoredArtifact(
            key, etag, request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        entry = self._objects.get(key)
        if entry is None or key in self.deleted:
            return None
        return artifact_types.HeadResult(size_bytes=len(entry[0]), checksum_sha256=None, etag="e")

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact:
        entry = self._objects.get(key)
        if entry is None or key in self.deleted:
            raise CategorizedError(
                f"artifact {key!r} is gone",
                category=ErrorCategory.STALE_HANDLE,
                details={"key": key},
            )
        return artifact_types.FetchedArtifact(entry[0], Sensitivity.QUARANTINED, "upload")

    # --- sweep port ---
    def list_image_objects(self) -> list[ObjectListing]:
        now = datetime.now(UTC)
        return [
            ObjectListing(key=key, last_modified=now - age)
            for key, (_data, age) in self._objects.items()
            if key not in self.deleted
        ]

    def head_present(self, key: str) -> bool:
        return key in self._objects and key not in self.deleted

    def delete(self, key: str) -> None:
        self.deleted.append(key)


def _conforming() -> InspectSeam:
    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return set(candidates)

    return _probe


def _missing(*absent: str) -> InspectSeam:
    absent_paths = {GUEST_CONTRACT_PATHS[a] for a in absent}

    def _probe(qcow2_path: Path, candidates: Sequence[str]) -> set[str]:
        return {c for c in candidates if c not in absent_paths}

    return _probe


def _quarantine(store: _FakeImageStore, key: str, payload: bytes) -> None:
    store._objects[key] = (payload, timedelta())  # noqa: SLF001 - seed the quarantined object


async def _register(
    conn: psycopg.AsyncConnection,
    store: _FakeImageStore,
    *,
    project: str = _PROJECT,
    name: str = "myrootfs",
    quarantine_key: str = "uploads/q/proj/rootfs.qcow2",
    expires_at: datetime | None = None,
    inspect: InspectSeam | None = None,
):
    return await register_private_upload(
        conn,
        store,
        project=project,
        principal=_PRINCIPAL,
        name=name,
        provider="local-libvirt",
        arch="x86_64",
        quarantine_key=quarantine_key,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(days=3)),
        required=_REQUIRED,
        inspect=inspect or _conforming(),
    )


async def _publish_public(
    conn: psycopg.AsyncConnection, store: _FakeImageStore, tmp: Path, *, name: str, payload: bytes
):
    src = tmp / f"{name}.qcow2"
    src.write_bytes(payload)
    return await publish_image(
        conn,
        store,
        request=PublishRequest(
            provider="local-libvirt",
            name=name,
            arch="x86_64",
            format="qcow2",
            root_device="/dev/vda",
            digest="sha256:" + hashlib.sha256(payload).hexdigest(),
            capabilities=(),
            provenance={},
            visibility=ImageVisibility.PUBLIC,
        ),
        source=src,
    )


async def _row_count(conn: psycopg.AsyncConnection, row_id) -> int:
    cur = await conn.execute("SELECT count(*) FROM image_catalog WHERE id = %s", (row_id,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _denial_rows(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM audit_log WHERE tool = %s AND transition = 'denied'",
        (_UPLOAD_TOOL,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


# ---- exit criterion 2: each half-published state is reconciled (inject + sweep) -----


def test_half_published_object_without_row_is_reconciled(migrated_url: str) -> None:
    # Inject the object-without-row half-state (a build that wrote bytes before any row, or a
    # crashed publish) past the grace, then SWEEP: repair_leaked_images deletes it. A fresh
    # object inside the grace is the negative control proving the sweep is not unconditional.
    async def _run() -> None:
        leaked = "images/local-libvirt/orphan/x86_64.qcow2"
        fresh = "images/local-libvirt/inflight/x86_64.qcow2"
        store = _FakeImageStore(
            {leaked: (b"orphan", timedelta(hours=2)), fresh: (b"inflight", timedelta(minutes=5))}
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: repair_leaked_images(c, store, _GRACE))
        assert count == 1
        assert store.deleted == [leaked]  # the in-grace object is protected, not swept

    asyncio.run(_run())


def test_half_published_row_without_object_is_reconciled(migrated_url: str) -> None:
    # Inject the row-without-object half-state (a crashed publish whose object never landed) past
    # its publish deadline, then SWEEP: repair_dangling_images removes the row. An object-less
    # `defined` baseline (object-less by design) is the negative control: it is SKIPPED.
    async def _run() -> None:
        dangling_key = "images/local-libvirt/gone/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            dangling_id = await _insert_row(
                seed, name="gone", state="pending", object_key=dangling_key, pending_age_hours=2
            )
            defined_id = await _insert_row(
                seed, name="baseline", state="defined", object_key=None, pending_age_hours=5
            )
        store = _FakeImageStore({})  # neither object is present
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: repair_dangling_images(c, store, _GRACE))
        assert count == 1  # only the dangling row is removed
        async with await connect(migrated_url) as check:
            assert await _row_count(check, dangling_id) == 0
            assert await _row_count(check, defined_id) == 1  # the defined baseline survives

    asyncio.run(_run())


async def _insert_row(
    conn: psycopg.AsyncConnection,
    *,
    name: str,
    state: str,
    object_key: str | None,
    pending_age_hours: float,
    visibility: str = "public",
    owner: str | None = None,
    expires_in_seconds: float | None = None,
):
    expires = (
        "now() + make_interval(secs => %(expires_secs)s)"
        if expires_in_seconds is not None
        else "NULL"
    )
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        " expires_at, state, pending_since) "
        "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(object_key)s, "
        " %(digest)s, %(visibility)s, %(owner)s, "
        f"{expires}, %(state)s, now() - make_interval(secs => %(pending_secs)s)) RETURNING id",
        {
            "name": name,
            "object_key": object_key,
            "digest": None if object_key is None else "sha256:abc",
            "visibility": visibility,
            "owner": owner,
            "state": state,
            "pending_secs": pending_age_hours * 3600,
            "expires_secs": (expires_in_seconds or 0.0),
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


# ---- exit criterion 3: private isolation; auto-prune; reference guard ---------------


def test_private_upload_resolves_only_within_owning_project(
    migrated_url: str, tmp_path: Path
) -> None:
    # A registered private upload resolves for its owning project and shadows a same-identity
    # public image there; another project resolves only the public one (never the private).
    async def _run() -> None:
        store = _FakeImageStore()
        _quarantine(store, "uploads/q/proj/rootfs.qcow2", b"private-rootfs")
        async with await connect(migrated_url) as conn:
            await _publish_public(conn, store, tmp_path, name="myrootfs", payload=b"public-rootfs")
            private = await _register(conn, store)
            assert private.visibility is ImageVisibility.PRIVATE
            mine = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project=_PROJECT)
            other = await resolve_rootfs(conn, "local-libvirt", "myrootfs", project="other")
            assert mine is not None and mine.id == private.id
            assert other is not None and other.visibility is ImageVisibility.PUBLIC

    asyncio.run(_run())


def test_expired_private_image_is_auto_pruned(migrated_url: str) -> None:
    # An expired, unreferenced private image is auto-pruned by the reconciler sweep (object + row).
    async def _run() -> None:
        key = "images/local-libvirt__proj/expired/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_row(
                seed,
                name="expired",
                state="registered",
                object_key=key,
                pending_age_hours=2,
                visibility="private",
                owner=_PROJECT,
                expires_in_seconds=-1,  # already expired
            )
        store = _FakeImageStore({key: (b"expired-rootfs", timedelta(hours=2))})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: repair_expired_private_images(c, store))
        assert count == 1
        assert store.deleted == [key]
        async with await connect(migrated_url) as check:
            assert await _row_count(check, row_id) == 0

    asyncio.run(_run())


def test_expired_private_referenced_by_live_system_is_not_pruned(migrated_url: str) -> None:
    # The reference guard: an expired private image a non-terminal System still references via its
    # provisioning_profile catalog rootfs is NOT pruned — its expiry defers.
    async def _run() -> None:
        key = "images/local-libvirt__proj/used/x86_64.qcow2"
        async with await connect(migrated_url) as seed:
            row_id = await _insert_row(
                seed,
                name="used",
                state="registered",
                object_key=key,
                pending_age_hours=2,
                visibility="private",
                owner=_PROJECT,
                expires_in_seconds=-1,
            )
            system_id = await seed_system(seed)  # READY: non-terminal
            await _reference_image(seed, system_id, name="used")
        store = _FakeImageStore({key: (b"used-rootfs", timedelta(hours=2))})
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: repair_expired_private_images(c, store))
        assert count == 0  # the live reference defers the prune
        assert store.deleted == []
        async with await connect(migrated_url) as check:
            assert await _row_count(check, row_id) == 1

    asyncio.run(_run())


async def _reference_image(conn: psycopg.AsyncConnection, system_id, *, name: str) -> None:
    """Point a System's provisioning_profile at the catalog rootfs ``(local-libvirt, name)``."""
    from psycopg.types.json import Jsonb

    profile = {
        "version": 1,
        "arch": "x86_64",
        "vcpu": 1,
        "memory_mb": 1024,
        "disk_gb": 10,
        "boot_method": "kexec",
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "catalog", "provider": "local-libvirt", "name": name}
            }
        },
    }
    await conn.execute(
        "UPDATE systems SET provisioning_profile = %s WHERE id = %s", (Jsonb(profile), system_id)
    )


# ---- exit criterion 4: non-conforming rejected (named reason) + over-quota denied; audited --


def test_non_conforming_upload_is_rejected_with_named_reason(migrated_url: str) -> None:
    # An upload missing a guest-contract element is rejected with the element named, while still
    # quarantined: no catalog row, the object never leaves quarantine.
    from kdive.db.repositories import IMAGE_CATALOG

    async def _run() -> None:
        store = _FakeImageStore()
        _quarantine(store, "uploads/q/proj/rootfs.qcow2", b"missing-agent-rootfs")
        async with await connect(migrated_url) as conn:
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, inspect=_missing("agent"))
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert "agent" in str(err.value)
            assert err.value.details.get("missing") == "agent"  # the named reason
            assert await IMAGE_CATALOG.list_all(conn) == []  # never registered
            assert store.puts == []  # never left quarantine

    asyncio.run(_run())


def test_over_quota_upload_is_denied(migrated_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # An upload over the per-project count cap is denied fail-closed AND the denial is audited.
    from kdive.config.core_settings import IMAGE_PRIVATE_MAX_COUNT
    from kdive.db.repositories import IMAGE_CATALOG

    monkeypatch.setenv(IMAGE_PRIVATE_MAX_COUNT.name, "1")

    async def _run() -> None:
        store = _FakeImageStore()
        _quarantine(store, "uploads/q/proj/a.qcow2", b"rootfs-a")
        _quarantine(store, "uploads/q/proj/b.qcow2", b"rootfs-b")
        async with await connect(migrated_url) as conn:
            await _register(conn, store, name="first", quarantine_key="uploads/q/proj/a.qcow2")
            before = await _denial_rows(conn)
            with pytest.raises(CategorizedError) as err:
                await _register(conn, store, name="second", quarantine_key="uploads/q/proj/b.qcow2")
            assert err.value.category is ErrorCategory.QUOTA_EXCEEDED
            registered = [r for r in await IMAGE_CATALOG.list_all(conn) if r.name == "second"]
            assert registered == []  # fail-closed: the over-cap image is not registered
            assert await _denial_rows(conn) == before + 1  # the denial is audited

    asyncio.run(_run())


# ---- exit criterion 1 + 5 meta-guards ----------------------------------------------


def test_criterion_1_regression_lives_with_both_kernel_planes() -> None:
    # Criterion 1 (a no-op kernel patch fails patch-applied verification) is proven co-located
    # with each kernel build plane's _apply_patch. Pin that both regressions exist so the class
    # stays closed for BOTH planes (the #227 class is per-plane, not a shared helper).
    root = pathlib.Path(__file__).resolve().parents[2]
    name = "def test_exit_criterion_noop_patch_fails_patch_applied_verification"
    for plane in ("local_libvirt", "remote_libvirt"):
        text = (root / "tests" / "providers" / plane / "test_build.py").read_text(encoding="utf-8")
        assert name in text, f"missing the criterion-1 no-op regression for the {plane} plane"


def test_exit_criterion_proof_is_ci_tier() -> None:
    # This proof is CI-tier: it carries no live_stack/live_vm marker, so it runs in normal CI
    # against the disposable-Postgres fixture (the store + libguestfs inspect are faked). The
    # criterion-5 operator-run rootfs build through the Python plane on the live stack is
    # env-gated (KDIVE_LIVE_SSH_TARGET) and recorded in docs/runbooks/image-lifecycle.md.
    lines = pathlib.Path(__file__).read_text(encoding="utf-8").splitlines()
    decorators = [line.strip() for line in lines if line.lstrip().startswith("@")]
    assert not any("live_stack" in d or "live_vm" in d for d in decorators)
