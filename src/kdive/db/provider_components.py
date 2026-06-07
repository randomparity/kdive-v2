"""Provider component registry helpers (ADR-0065)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Literal, NamedTuple, Protocol, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.references import ComponentRef, parse_component_ref
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import HeadResult

type Visibility = Literal["public", "project", "host-policy"]


class ProviderComponent(NamedTuple):
    id: UUID
    provider: str
    component_kind: str
    source: ComponentRef
    artifact_id: UUID | None
    visibility: str
    project: str | None
    principal: str
    sha256: str | None


class UploadVerifier(Protocol):
    def head(self, key: str) -> HeadResult | None:
        """Return uploaded object metadata, or None when the object is absent."""


async def link_local_component(
    pool: AsyncConnectionPool,
    *,
    provider: str,
    component_kind: str,
    path: str,
    sha256: str,
    visibility: Visibility,
    project: str | None,
    principal: str,
) -> UUID:
    source = {"kind": "local", "path": path, "sha256": sha256}
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO provider_components "
            "(provider, component_kind, source, visibility, project, principal, sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (provider, component_kind, Jsonb(source), visibility, project, principal, sha256),
        )
        found = await row.fetchone()
    assert found is not None
    return found[0]


async def create_artifact_component(
    pool: AsyncConnectionPool,
    *,
    provider: str,
    component_kind: str,
    artifact_id: UUID,
    sha256: str,
    visibility: Visibility,
    project: str | None,
    principal: str,
) -> UUID:
    source = {"kind": "artifact", "artifact_id": str(artifact_id), "sha256": sha256}
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO provider_components "
            "(provider, component_kind, source, artifact_id, visibility, project, "
            "principal, sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                provider,
                component_kind,
                Jsonb(source),
                artifact_id,
                visibility,
                project,
                principal,
                sha256,
            ),
        )
        found = await row.fetchone()
    assert found is not None
    return found[0]


async def get_visible_component(
    pool: AsyncConnectionPool,
    component_id: UUID,
    *,
    project: str,
) -> ProviderComponent | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM provider_components WHERE id = %s "
            "AND (visibility = 'public' OR project = %s)",
            (component_id, project),
        )
        row = await cur.fetchone()
    return None if row is None else _component_from_row(row)


async def list_visible_components(
    pool: AsyncConnectionPool,
    *,
    provider: str,
    component_kind: str,
    project: str,
) -> list[ProviderComponent]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM provider_components "
            "WHERE provider = %s AND component_kind = %s "
            "AND (visibility = 'public' OR project = %s) ORDER BY created_at, id",
            (provider, component_kind, project),
        )
        rows = await cur.fetchall()
    return [_component_from_row(row) for row in rows]


async def create_component_upload_intent(
    pool: AsyncConnectionPool,
    *,
    tenant: str,
    provider: str,
    component_kind: str,
    sha256: str,
    size_bytes: int,
    visibility: Literal["public", "project"],
    project: str,
    principal: str,
    ttl: timedelta = timedelta(hours=1),
) -> tuple[UUID, str]:
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO component_uploads "
            "(provider, component_kind, sha256, size_bytes, visibility, project, principal, "
            "state, deadline) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', now() + %s) "
            "RETURNING id",
            (provider, component_kind, sha256, size_bytes, visibility, project, principal, ttl),
        )
        found = await row.fetchone()
    assert found is not None
    upload_id = found[0]
    return upload_id, component_upload_object_key(
        tenant=tenant,
        provider=provider,
        component_kind=component_kind,
        upload_id=upload_id,
    )


async def finalize_component_upload(
    pool: AsyncConnectionPool,
    upload_id: UUID,
    *,
    object_store: UploadVerifier,
) -> UUID:
    async with pool.connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM component_uploads WHERE id = %s FOR UPDATE",
                (upload_id,),
            )
            upload = await cur.fetchone()
        if upload is None:
            raise CategorizedError(
                "component upload does not exist",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        existing = upload["artifact_id"]
        if existing is not None:
            return existing

        key = component_upload_object_key(
            tenant=upload["project"],
            provider=upload["provider"],
            component_kind=upload["component_kind"],
            upload_id=upload_id,
        )
        head = object_store.head(key)
        if (
            head is None
            or head.size_bytes != upload["size_bytes"]
            or head.checksum_sha256 != upload["sha256"]
        ):
            raise CategorizedError(
                "component upload object does not match its intent",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        source = {
            "kind": "artifact",
            "artifact_id": str(upload_id),
            "sha256": upload["sha256"],
        }
        row = await conn.execute(
            "INSERT INTO provider_components "
            "(provider, component_kind, source, artifact_id, visibility, project, "
            "principal, sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                upload["provider"],
                upload["component_kind"],
                Jsonb(source),
                upload_id,
                upload["visibility"],
                upload["project"],
                upload["principal"],
                upload["sha256"],
            ),
        )
        inserted = await row.fetchone()
        assert inserted is not None
        component_id = inserted[0]
        await conn.execute(
            "UPDATE component_uploads SET artifact_id = %s, state = 'finalized' WHERE id = %s",
            (component_id, upload_id),
        )
        return component_id


def component_upload_object_key(
    *,
    tenant: str,
    provider: str,
    component_kind: str,
    upload_id: UUID,
) -> str:
    return f"{tenant}/provider-components/{provider}/{component_kind}/{upload_id}"


def _component_from_row(row: dict[str, object]) -> ProviderComponent:
    try:
        source = parse_component_ref(cast(Mapping[str, object], row["source"]))
    except CategorizedError as exc:
        raise CategorizedError(
            "stored provider component source is invalid",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    return ProviderComponent(
        id=cast(UUID, row["id"]),
        provider=cast(str, row["provider"]),
        component_kind=cast(str, row["component_kind"]),
        source=source,
        artifact_id=cast(UUID | None, row["artifact_id"]),
        visibility=cast(str, row["visibility"]),
        project=cast(str | None, row["project"]),
        principal=cast(str, row["principal"]),
        sha256=cast(str | None, row["sha256"]),
    )
