"""Server-side ``images.*`` operator/admin tools (M2.4/7, ADR-0092/0093, issue #288).

Handlers are driven directly with an injected pool + RequestContext (the repo unit
contract). Coverage maps to the issue's falsifiable acceptance:

* ``images.build``/``images.publish`` authorize as ``platform_operator`` (NOT
  ``platform_admin`` — the role order is not a total hierarchy) and audit to
  ``platform_audit_log``; a non-operator (including a bare ``platform_admin``) is denied
  and audited before any pool mutation;
* ``images.delete`` is project-scoped (an ``operator`` on the image's owning project);
  a member-over-reach caller is denied and audited; a non-member is denied without writing
  an audit row under an ungranted project, and the catalog row survives;
* ``images.prune_expired``/``images.extend`` route the ``platform_admin`` break-glass
  path (NOT the per-allocation gate); a ``platform_operator`` is denied and audited;
* every authorized mutating call writes one ``platform_audit_log`` row before/independent
  of the catalog mutation.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest
from fastmcp import FastMCP
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.config.core_settings import IMAGE_PRIVATE_LIFETIME_MAX
from kdive.domain.errors import ErrorCategory
from kdive.domain.state import SystemState
from kdive.jobs.payloads import ImageBuildPayload
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops import images as ops_images
from kdive.mcp.tools.ops.images import registrar as images_registrar
from kdive.mcp.tools.ops.images._common import (
    DELETE_TOOL,
    EXTEND_TOOL,
    PRUNE_TOOL,
    UPLOAD_TOOL,
)
from kdive.mcp.tools.ops.images.build_publish import BUILD_TOOL, PUBLISH_TOOL
from kdive.reconciler.images import ImageMtime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.images.upload import UploadObjectStore
from tests.reconciler.conftest import connect, seed_system

_TARGET_PROJECT = "tenant-x"
_DELETE_MODULE = importlib.import_module("kdive.mcp.tools.ops.images.delete")


class _FakeImageStore:
    """A narrow expired-private sweep store stand-in (deletes are recorded, never real)."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def list_image_objects(self) -> list[ImageMtime]:
        return []

    def head_present(self, key: str) -> bool:
        return key not in self.deleted

    def delete(self, key: str) -> None:
        self.deleted.append(key)


class _UnusedUploadStore:
    """A no-op UploadObjectStore stand-in for paths that reject before any store call.

    Every method raises: the tests using it exercise a guard that returns before any store
    call, so a touch is a test bug. A ``cast`` at the call site satisfies the protocol type.
    """

    def put_artifact(self, request: object) -> object:
        raise AssertionError("store must not be touched")

    def head(self, key: str) -> object:
        raise AssertionError("store must not be touched")

    def get_artifact(self, key: str, etag: str | None) -> object:
        raise AssertionError("store must not be touched")


def _admin_ctx(*, principal: str = "ops-admin") -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-admin",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )


def _operator_ctx(*, principal: str = "ops-operator") -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-op",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
    )


def _member_ctx(
    *, project: str = _TARGET_PROJECT, role: Role = Role.OPERATOR, principal: str = "dev-1"
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-dev",
        projects=(project,),
        roles={project: role},
        platform_roles=frozenset(),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_private_image(
    pool: AsyncConnectionPool,
    *,
    owner: str = _TARGET_PROJECT,
    name: str = "custom",
    expires_in: timedelta = timedelta(hours=1),
) -> UUID:
    object_key = f"images/local-libvirt__{owner}/{name}/x86_64.qcow2"
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
            " expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " 'sha256:abc', 'private', %(owner)s, now() + make_interval(secs => %(secs)s), "
            " 'registered', now()) RETURNING id",
            {
                "name": name,
                "key": object_key,
                "owner": owner,
                "secs": expires_in.total_seconds(),
            },
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_public_image(pool: AsyncConnectionPool, *, name: str = "fedora") -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, "
            " owner, expires_at, state, pending_since) "
            "VALUES ('local-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(key)s, "
            " 'sha256:public', 'public', NULL, NULL, 'registered', now()) RETURNING id",
            {"name": name, "key": f"images/local-libvirt/{name}/x86_64.qcow2"},
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _platform_audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope FROM platform_audit_log ORDER BY id"
        )
        return list(await cur.fetchall())


async def _audit_log_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, project, tool, transition FROM audit_log ORDER BY ts, id"
        )
        return list(await cur.fetchall())


async def _image_exists(url: str, image_id: UUID) -> bool:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM image_catalog WHERE id = %s", (image_id,))
        return await cur.fetchone() is not None


async def _image_expires_at(url: str, image_id: UUID) -> datetime:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT expires_at FROM image_catalog WHERE id = %s", (image_id,))
        row = await cur.fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, datetime)
    return value


async def _job_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute("SELECT kind, dedup_key FROM jobs ORDER BY id")
        return list(await cur.fetchall())


def _destructive_hint(tool: object) -> bool | None:
    annotations = getattr(tool, "annotations", None)
    value = getattr(annotations, "destructiveHint", None)
    return value if isinstance(value, bool) else None


async def _call_registered_tool(tool: object, *args: object) -> ToolResponse:
    fn = cast(Any, tool).fn
    result = await fn(*args)
    assert isinstance(result, ToolResponse)
    return result


def _build(pool: AsyncConnectionPool, ctx: RequestContext):
    return ops_images.build(
        pool,
        ctx,
        payload=ImageBuildPayload(
            provider="local-libvirt",
            name="fedora-40",
            arch="x86_64",
            releasever="40",
            source_image_digest="sha256:base",
            capabilities=("agent", "kdump"),
            format="qcow2",
            root_device="/dev/vda",
        ),
    )


def _publish(pool: AsyncConnectionPool, ctx: RequestContext):
    return ops_images.publish(
        pool,
        ctx,
        payload=ImageBuildPayload(
            provider="local-libvirt",
            name="fedora-40",
            arch="x86_64",
            releasever="40",
            source_image_digest="sha256:base",
            capabilities=("agent", "kdump"),
            format="qcow2",
            root_device="/dev/vda",
        ),
    )


# --- registrar boundary --------------------------------------------------------------------


def test_images_registrar_exposes_annotations_and_invokes_wrappers(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            ctx = {"value": _operator_ctx()}
            monkeypatch.setattr(images_registrar, "current_context", lambda: ctx["value"])
            app = FastMCP("images-registrar-test")
            images_registrar.register(
                app,
                pool,
                image_store=_FakeImageStore(),
                upload_store=cast(UploadObjectStore, _UnusedUploadStore()),
            )
            tools = {tool.name: tool for tool in await app.list_tools()}

            assert set(tools) == {
                BUILD_TOOL,
                PUBLISH_TOOL,
                UPLOAD_TOOL,
                DELETE_TOOL,
                PRUNE_TOOL,
                EXTEND_TOOL,
            }
            assert _destructive_hint(tools[BUILD_TOOL]) is False
            assert _destructive_hint(tools[PRUNE_TOOL]) is True

            request = images_registrar.ImageBuildRequest(
                provider="local-libvirt",
                name="fedora-40",
                arch="x86_64",
                releasever="40",
                source_image_digest="sha256:base",
                capabilities=("agent", "kdump"),
            )
            build_resp = await _call_registered_tool(tools[BUILD_TOOL], request)
            ctx["value"] = _admin_ctx()
            prune_resp = await _call_registered_tool(tools[PRUNE_TOOL], "registrar boundary")

        assert build_resp.status not in {"error", "failed"}
        assert prune_resp.status == "pruned"
        assert [kind for kind, _ in await _job_rows(migrated_url)] == ["image_build"]

    asyncio.run(_run())


# --- build / publish: platform_operator gate ------------------------------------------------


def test_build_operator_enqueues_image_build_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _build(pool, _operator_ctx())
        assert resp.status not in {"error", "failed"}
        jobs = await _job_rows(migrated_url)
        assert [kind for kind, _ in jobs] == ["image_build"]
        audit = await _platform_audit_rows(migrated_url)
        assert audit == [
            ("ops-operator", "platform_operator", "images.build", "local-libvirt:fedora-40")
        ]

    asyncio.run(_run())


def test_build_admin_without_operator_is_denied_and_audited(migrated_url: str) -> None:
    # platform_admin does NOT imply platform_operator: the build gate must reject it.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _build(pool, _admin_ctx())
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _job_rows(migrated_url) == []
        audit = await _platform_audit_rows(migrated_url)
        assert audit == [("ops-admin", "platform_admin", "images.build", "denied:fedora-40")]

    asyncio.run(_run())


def test_build_unprivileged_denied_audited_no_pool_mutation(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _build(pool, _member_ctx())
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _job_rows(migrated_url) == []
        # A project-only token holds no platform role, so the platform-denial recorder
        # does not write a platform_audit_log row (matching ops siblings).
        assert await _platform_audit_rows(migrated_url) == []

    asyncio.run(_run())


def test_publish_operator_enqueues_image_build_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _publish(pool, _operator_ctx())
        assert resp.status not in {"error", "failed"}
        assert [kind for kind, _ in await _job_rows(migrated_url)] == ["image_build"]
        audit = await _platform_audit_rows(migrated_url)
        assert audit[0][2] == "images.publish"

    asyncio.run(_run())


def test_publish_operator_denied_for_admin(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _publish(pool, _admin_ctx())
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _job_rows(migrated_url) == []

    asyncio.run(_run())


# --- delete: project-scoped operator role ---------------------------------------------------


def test_delete_project_operator_removes_row_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool)
            resp = await ops_images.delete(
                pool, _member_ctx(role=Role.OPERATOR), image_id=str(image_id)
            )
        assert resp.status not in {"error", "failed"}
        assert await _image_exists(migrated_url, image_id) is False
        audit = await _audit_log_rows(migrated_url)
        assert audit and audit[-1][1] == _TARGET_PROJECT and audit[-1][2] == "images.delete"

    asyncio.run(_run())


def test_delete_rejects_invalid_or_missing_image(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            invalid = await ops_images.delete(
                pool, _member_ctx(role=Role.OPERATOR), image_id="not-a-uuid"
            )
            missing = await ops_images.delete(
                pool, _member_ctx(role=Role.OPERATOR), image_id=str(UUID(int=1))
            )
        assert invalid.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert missing.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


def test_delete_rejects_public_image_without_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_public_image(pool)
            resp = await ops_images.delete(
                pool, _member_ctx(role=Role.OPERATOR), image_id=str(image_id)
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _image_exists(migrated_url, image_id) is True
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


def test_delete_owned_is_idempotent_when_locked_row_is_gone(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _DELETE_MODULE._delete_owned(
                pool, _member_ctx(role=Role.OPERATOR), UUID(int=1), project=_TARGET_PROJECT
            )
        assert resp.status == "deleted"
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


def test_delete_member_overreach_denied_and_audited(migrated_url: str) -> None:
    # A viewer on the image's project lacks operator: denied, audited, row survives.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool)
            resp = await ops_images.delete(
                pool, _member_ctx(role=Role.VIEWER), image_id=str(image_id)
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _image_exists(migrated_url, image_id) is True
        audit = await _audit_log_rows(migrated_url)
        assert audit == [("dev-1", _TARGET_PROJECT, "images.delete", "denied")]

    asyncio.run(_run())


def test_delete_cross_project_denied_without_project_audit(migrated_url: str) -> None:
    # A caller who is not a member of the image's owning project is denied, but must not
    # create an audit_log row under a project absent from ctx.projects.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, owner="tenant-x")
            resp = await ops_images.delete(
                pool,
                _member_ctx(project="tenant-y", role=Role.OPERATOR),
                image_id=str(image_id),
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _image_exists(migrated_url, image_id) is True
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


async def _reference_image(url: str, *, provider: str, name: str) -> None:
    """Seed a non-terminal System whose provisioning_profile references ``(provider, name)``."""
    profile = {
        "provider": {
            "local-libvirt": {"rootfs": {"kind": "catalog", "provider": provider, "name": name}}
        }
    }
    async with await connect(url) as conn:
        system_id = await seed_system(conn, system_state=SystemState.READY)
        await conn.execute(
            "UPDATE systems SET provisioning_profile = %s WHERE id = %s",
            (Jsonb(profile), system_id),
        )


def test_delete_declines_a_referenced_image(migrated_url: str) -> None:
    # A private image a non-terminal System still boots from must NOT be deletable — the
    # operator delete honors the same reference guard the reconciler's auto-prune uses.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, name="referenced")
            await _reference_image(migrated_url, provider="local-libvirt", name="referenced")
            resp = await ops_images.delete(
                pool, _member_ctx(role=Role.OPERATOR), image_id=str(image_id)
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _image_exists(migrated_url, image_id) is True

    asyncio.run(_run())


def test_upload_unprivileged_denied_audited_even_without_store(migrated_url: str) -> None:
    # The authz boundary is evaluated before the store-availability check, so an
    # unprivileged caller is denied and audited even on an S3-less deployment.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_images.upload(
                pool,
                _member_ctx(role=Role.VIEWER),
                None,
                ops_images.ImageUploadRequest(
                    project=_TARGET_PROJECT,
                    name="custom",
                    arch="x86_64",
                    quarantine_key="quarantine/abc",
                ),
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        audit = await _audit_log_rows(migrated_url)
        assert audit == [("dev-1", _TARGET_PROJECT, "images.upload", "denied")]

    asyncio.run(_run())


def test_upload_cross_project_denied_without_project_audit(migrated_url: str) -> None:
    # Non-members are denied before store access, but must not write audit_log rows under
    # arbitrary requested project names.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_images.upload(
                pool,
                _member_ctx(project="tenant-y", role=Role.OPERATOR),
                None,
                ops_images.ImageUploadRequest(
                    project=_TARGET_PROJECT,
                    name="custom",
                    arch="x86_64",
                    quarantine_key="quarantine/abc",
                ),
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert await _audit_log_rows(migrated_url) == []

    asyncio.run(_run())


def test_upload_rejects_quarantine_key_in_published_prefix(migrated_url: str) -> None:
    # A quarantine_key under the published images/ prefix would let an operator re-ingest
    # another project's owner-scoped private image — it is rejected with a config error.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await ops_images.upload(
                pool,
                _member_ctx(role=Role.OPERATOR),
                cast(UploadObjectStore, _UnusedUploadStore()),
                ops_images.ImageUploadRequest(
                    project=_TARGET_PROJECT,
                    name="evil",
                    arch="x86_64",
                    quarantine_key="images/local-libvirt__tenant-y/secret/x86_64.qcow2",
                ),
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


# --- prune_expired / extend: platform_admin break-glass -------------------------------------


def test_prune_expired_admin_runs_sweep_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            expired = await _insert_private_image(pool, expires_in=timedelta(seconds=-3600))
            resp = await ops_images.prune_expired(
                pool, _admin_ctx(), reason="cleanup", image_store=_FakeImageStore()
            )
        assert resp.status not in {"error", "failed"}
        assert resp.data["pruned"] == "1"
        assert await _image_exists(migrated_url, expired) is False
        audit = await _platform_audit_rows(migrated_url)
        assert audit and audit[0][2] == "images.prune_expired"

    asyncio.run(_run())


def test_prune_expired_blank_reason_rejects_before_sweep_or_audit(migrated_url: str) -> None:
    store = _FakeImageStore()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            expired = await _insert_private_image(pool, expires_in=timedelta(seconds=-3600))
            resp = await ops_images.prune_expired(
                pool, _admin_ctx(), reason="  ", image_store=store
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _image_exists(migrated_url, expired) is True
        assert store.deleted == []
        assert await _platform_audit_rows(migrated_url) == []

    asyncio.run(_run())


def test_prune_expired_operator_denied_and_audited(migrated_url: str) -> None:
    # platform_operator does NOT satisfy the platform_admin break-glass gate.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_private_image(pool, expires_in=timedelta(seconds=-3600))
            resp = await ops_images.prune_expired(
                pool, _operator_ctx(), reason="cleanup", image_store=_FakeImageStore()
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        audit = await _platform_audit_rows(migrated_url)
        assert audit == [
            ("ops-operator", "platform_operator", "images.prune_expired", "denied:all-private")
        ]

    asyncio.run(_run())


def test_extend_admin_rearms_expiry_and_audits(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, expires_in=timedelta(minutes=1))
            before = await _image_expires_at(migrated_url, image_id)
            resp = await ops_images.extend(
                pool, _admin_ctx(), image_id=str(image_id), seconds=86400, reason="keep"
            )
            after = await _image_expires_at(migrated_url, image_id)
        assert resp.status not in {"error", "failed"}
        assert after > before
        audit = await _platform_audit_rows(migrated_url)
        assert audit and audit[0][2] == "images.extend"

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("seconds", "reason"),
    [
        (0, "keep"),
        (-1, "keep"),
        (3600, " "),
    ],
)
def test_extend_rejects_invalid_seconds_or_reason_before_audit(
    migrated_url: str, seconds: int, reason: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, expires_in=timedelta(minutes=1))
            before = await _image_expires_at(migrated_url, image_id)
            resp = await ops_images.extend(
                pool, _admin_ctx(), image_id=str(image_id), seconds=seconds, reason=reason
            )
            after = await _image_expires_at(migrated_url, image_id)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert after == before
        assert await _platform_audit_rows(migrated_url) == []

    asyncio.run(_run())


def test_extend_rejects_public_image_after_breakglass_audit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_public_image(pool)
            resp = await ops_images.extend(
                pool, _admin_ctx(), image_id=str(image_id), seconds=3600, reason="keep"
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert await _image_exists(migrated_url, image_id) is True
        audit = await _platform_audit_rows(migrated_url)
        assert audit == [("ops-admin", "platform_admin", "images.extend", f"image:{image_id}")]

    asyncio.run(_run())


def test_extend_clamps_to_lifetime_ceiling(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extend ceiling is the per-image lifetime max; a request past it is clamped.
    max_seconds = 120
    requested_seconds = 10 * 365 * 24 * 3600
    monkeypatch.setenv(IMAGE_PRIVATE_LIFETIME_MAX.name, str(max_seconds))

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, expires_in=timedelta(minutes=1))
            before = datetime.now(UTC)
            resp = await ops_images.extend(
                pool,
                _admin_ctx(),
                image_id=str(image_id),
                seconds=requested_seconds,
                reason="forever",
            )
            after = await _image_expires_at(migrated_url, image_id)
        requested_deadline = before + timedelta(seconds=requested_seconds)
        clamped_floor = before + timedelta(seconds=max_seconds)
        clamped_ceiling = datetime.now(UTC) + timedelta(seconds=max_seconds)

        assert resp.status not in {"error", "failed"}
        assert clamped_floor <= after <= clamped_ceiling
        assert after < requested_deadline - timedelta(days=1)

    asyncio.run(_run())


def test_extend_operator_denied_and_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            image_id = await _insert_private_image(pool, expires_in=timedelta(minutes=1))
            resp = await ops_images.extend(
                pool, _operator_ctx(), image_id=str(image_id), seconds=3600, reason="x"
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())
