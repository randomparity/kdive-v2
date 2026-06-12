"""IMAGE_BUILD worker handler: build -> guest-contract-validate -> publish (issue #285).

Pins: the success path (a registered row whose object HEADs), the offloaded blocking build, and
that a guest-contract validation failure dead-letters the job with a NAMED category through the
worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import IMAGE_CATALOG, JOBS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageState, JobKind, ResourceKind
from kdive.domain.state import JobState
from kdive.images.planes.base import RootfsBuildOutput, RootfsBuildSpec
from kdive.images.validation import GUEST_CONTRACT_PATHS
from kdive.jobs import queue
from kdive.jobs.handlers.image_build import image_build_handler, register_handlers
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing, ImageBuildPayload
from kdive.jobs.worker import Worker
from kdive.provider_components import artifacts as artifact_types
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.resolver import ProviderResolver
from kdive.providers.runtime import ProviderRuntime
from kdive.security.secrets.secret_registry import SecretRegistry

_AUTHORIZING = Authorizing(principal="op", agent_session=None, project="platform")


class _FakePlane:
    """A RootfsBuildPlane stand-in writing fixed qcow2 bytes and recording the calling thread."""

    def __init__(self, tmp_path: Path, *, qcow2: bytes = b"built-qcow2") -> None:
        self._tmp_path = tmp_path
        self._qcow2 = qcow2
        self.build_thread: int | None = None
        self.spec: RootfsBuildSpec | None = None

    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput:
        self.build_thread = threading.get_ident()
        self.spec = spec
        out = self._tmp_path / "out.qcow2"
        out.write_bytes(self._qcow2)
        digest = "sha256:" + hashlib.sha256(self._qcow2).hexdigest()
        return RootfsBuildOutput(qcow2_path=out, digest=digest, provenance={"releasever": "43"})


class _FakeStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        self._objects[request.key()] = request.data
        return artifact_types.StoredArtifact(
            request.key(), "etag", request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> artifact_types.HeadResult | None:
        data = self._objects.get(key)
        if data is None:
            return None
        return artifact_types.HeadResult(size_bytes=len(data), checksum_sha256=None, etag="etag")


def _all_present(_path: Path, candidates: Sequence[str]) -> set[str]:
    return set(candidates)


def _none_present(_path: Path, _candidates: Sequence[str]) -> set[str]:
    return set()


def _resolver_with_plane(plane: _FakePlane | None) -> ProviderResolver:
    runtime = ProviderRuntime(
        profile_policy=LocalLibvirtProfilePolicy(),
        provisioner=cast(Any, object()),
        builder=cast(Any, object()),
        installer=cast(Any, object()),
        booter=cast(Any, object()),
        connector=cast(Any, object()),
        controller=cast(Any, object()),
        retriever=cast(Any, object()),
        crash_postmortem=cast(Any, object()),
        vmcore_introspector=cast(Any, object()),
        live_introspector=cast(Any, object()),
        rootfs_build_plane=plane,
    )
    return ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})


def _payload(**kw: object) -> ImageBuildPayload:
    base: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "base",
        "arch": "x86_64",
        "releasever": "43",
        "packages": ("kexec-tools",),
        "source_image_digest": "sha256:" + "0" * 64,
        "capabilities": ("agent", "kdump", "drgn"),
        "format": "qcow2",
        "root_device": "/dev/vda",
        "visibility": "public",
    }
    base.update(kw)
    return ImageBuildPayload.model_validate(base)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    async with AsyncConnectionPool(url, min_size=2, max_size=10) as pool:
        yield pool


def test_handler_builds_validates_publishes_registered(migrated_url: str, tmp_path: Path) -> None:
    plane = _FakePlane(tmp_path)
    store = _FakeStore()

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            job = await queue.enqueue(
                conn, JobKind.IMAGE_BUILD, _payload(), _AUTHORIZING, "dedup-1"
            )
            main_thread = threading.get_ident()
            ref = await image_build_handler(
                conn, job, build_plane=plane, store=store, inspect=_all_present
            )
            assert ref is not None
            rows = await IMAGE_CATALOG.list_all(conn)
            assert len(rows) == 1
            assert rows[0].state is ImageState.REGISTERED
            assert store.head(rows[0].object_key or "") is not None
            # The blocking build ran off the event-loop thread.
            assert plane.build_thread is not None
            assert plane.build_thread != main_thread
            # The build spec carried the payload's pinned inputs.
            assert plane.spec is not None
            assert plane.spec.releasever == "43"

    asyncio.run(_run())


def test_handler_resolves_build_plane_from_provider_runtime(
    migrated_url: str, tmp_path: Path
) -> None:
    plane = _FakePlane(tmp_path)
    store = _FakeStore()
    resolver = _resolver_with_plane(plane)

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            job = await queue.enqueue(
                conn, JobKind.IMAGE_BUILD, _payload(), _AUTHORIZING, "dedup-resolver"
            )
            ref = await image_build_handler(
                conn, job, provider_resolver=resolver, store=store, inspect=_all_present
            )

            assert ref == "images/local-libvirt/base/x86_64.qcow2"
            assert plane.spec is not None
            assert plane.spec.provider == ResourceKind.LOCAL_LIBVIRT.value

    asyncio.run(_run())


def test_handler_dead_letters_validation_failure_with_named_category(
    migrated_url: str, tmp_path: Path
) -> None:
    plane = _FakePlane(tmp_path)
    store = _FakeStore()
    registry = HandlerRegistry()
    register_handlers(registry, build_plane=plane, store=store, inspect=_none_present)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.IMAGE_BUILD,
                    _payload(),
                    _AUTHORIZING,
                    "dedup-fail",
                    max_attempts=1,
                )
            worker = Worker(pool, registry, worker_id="w1", secret_registry=SecretRegistry())
            await worker.run_once()
            async with pool.connection() as conn:
                final = await JOBS.get(conn, job.id)
                assert final is not None
                assert final.state is JobState.FAILED
                assert final.error_category is ErrorCategory.CONFIGURATION_ERROR
                # No registered image: validation rejected it before the publish completed.
                rows = await IMAGE_CATALOG.list_all(conn)
                assert all(r.state is not ImageState.REGISTERED for r in rows)

    asyncio.run(_run())


def test_handler_propagates_validation_category_when_called_directly(
    migrated_url: str, tmp_path: Path
) -> None:
    plane = _FakePlane(tmp_path)
    store = _FakeStore()

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            job = await queue.enqueue(
                conn, JobKind.IMAGE_BUILD, _payload(), _AUTHORIZING, "dedup-2"
            )
            with pytest.raises(CategorizedError) as err:
                await image_build_handler(
                    conn, job, build_plane=plane, store=store, inspect=_none_present
                )
            assert err.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert "agent" in str(err.value)

    asyncio.run(_run())


def test_register_handlers_binds_image_build_kind(tmp_path: Path) -> None:
    registry = HandlerRegistry()
    register_handlers(
        registry, build_plane=_FakePlane(tmp_path), store=_FakeStore(), inspect=_all_present
    )
    assert registry.get(JobKind.IMAGE_BUILD) is not None
    assert GUEST_CONTRACT_PATHS  # the validator's contract map is importable for #286 reuse
