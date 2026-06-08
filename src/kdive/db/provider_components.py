"""Provider component registry helpers (ADR-0065)."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal, NamedTuple, Protocol, cast
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.components.local_paths import validate_local_component_path
from kdive.components.references import ComponentRef, parse_component_ref
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.store.objectstore import HeadResult

type Visibility = Literal["public", "project", "host-policy"]
type UploadVisibility = Literal["public", "project"]


@dataclass(frozen=True, slots=True)
class ComponentRegistration:
    provider: str
    component_kind: str
    visibility: Visibility
    project: str | None
    principal: str


@dataclass(frozen=True, slots=True)
class LinkLocalComponentRequest:
    registration: ComponentRegistration
    path: str
    sha256: str
    allowed_roots: Iterable[Path]


@dataclass(frozen=True, slots=True)
class ArtifactComponentRequest:
    registration: ComponentRegistration
    artifact_id: UUID
    sha256: str


@dataclass(frozen=True, slots=True)
class ComponentUploadRegistration:
    tenant: str
    provider: str
    component_kind: str
    visibility: UploadVisibility
    project: str
    principal: str


@dataclass(frozen=True, slots=True)
class ComponentUploadIntentRequest:
    registration: ComponentUploadRegistration
    sha256: str
    size_bytes: int
    ttl: timedelta = timedelta(hours=1)


class ProviderComponent(NamedTuple):
    id: UUID
    provider: str
    component_kind: str
    source: ComponentRef
    artifact_id: UUID | None
    visibility: Visibility
    project: str | None
    principal: str
    sha256: str | None


class UploadVerifier(Protocol):
    def head(self, key: str) -> HeadResult | None:
        """Return uploaded object metadata, or None when the object is absent."""


async def link_local_component(
    pool: AsyncConnectionPool,
    request: LinkLocalComponentRequest,
) -> UUID:
    """Register a local-file provider component.

    Raises:
        CategorizedError: The path is outside ``allowed_roots`` or the digest is invalid.
    """
    registration = request.registration
    resolved = validate_local_component_path(
        request.path,
        allowed_roots=request.allowed_roots,
        sha256=request.sha256,
    )
    source = parse_component_ref({"kind": "local", "path": str(resolved), "sha256": request.sha256})
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO provider_components "
            "(provider, component_kind, source, visibility, project, principal, sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                registration.provider,
                registration.component_kind,
                Jsonb(source.model_dump(mode="json")),
                registration.visibility,
                registration.project,
                registration.principal,
                request.sha256,
            ),
        )
        found = await row.fetchone()
    return _inserted_id(found)


async def create_artifact_component(
    pool: AsyncConnectionPool,
    request: ArtifactComponentRequest,
) -> UUID:
    """Register an artifact-backed provider component.

    Raises:
        CategorizedError: The artifact component reference is invalid.
    """
    registration = request.registration
    source = parse_component_ref(
        {"kind": "artifact", "artifact_id": str(request.artifact_id), "sha256": request.sha256}
    )
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO provider_components "
            "(provider, component_kind, source, artifact_id, visibility, project, "
            "principal, sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                registration.provider,
                registration.component_kind,
                Jsonb(source.model_dump(mode="json")),
                request.artifact_id,
                registration.visibility,
                registration.project,
                registration.principal,
                request.sha256,
            ),
        )
        found = await row.fetchone()
    return _inserted_id(found)


async def get_visible_component(
    pool: AsyncConnectionPool,
    component_id: UUID,
    *,
    project: str,
) -> ProviderComponent | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM provider_components WHERE id = %s "
            "AND (visibility = 'public' OR (visibility = 'project' AND project = %s))",
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
            "AND (visibility = 'public' OR (visibility = 'project' AND project = %s)) "
            "ORDER BY created_at, id",
            (provider, component_kind, project),
        )
        rows = await cur.fetchall()
    return [_component_from_row(row) for row in rows]


async def create_component_upload_intent(
    pool: AsyncConnectionPool,
    request: ComponentUploadIntentRequest,
) -> tuple[UUID, str]:
    """Create a pending upload intent and return its object-store key.

    Raises:
        CategorizedError: The eventual upload cannot be finalized if its object metadata does
            not match this intent.
    """
    registration = request.registration
    async with pool.connection() as conn:
        row = await conn.execute(
            "INSERT INTO component_uploads "
            "(tenant, provider, component_kind, sha256, size_bytes, visibility, project, "
            "principal, state, deadline) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', now() + %s) RETURNING id",
            (
                registration.tenant,
                registration.provider,
                registration.component_kind,
                request.sha256,
                request.size_bytes,
                registration.visibility,
                registration.project,
                registration.principal,
                request.ttl,
            ),
        )
        found = await row.fetchone()
    upload_id = _inserted_id(found)
    return upload_id, component_upload_object_key(
        tenant=registration.tenant,
        provider=registration.provider,
        component_kind=registration.component_kind,
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
                "SELECT component_uploads.*, deadline < now() AS expired "
                "FROM component_uploads WHERE id = %s FOR UPDATE",
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
        if upload["state"] != "pending" or upload["expired"]:
            raise CategorizedError(
                "component upload is not pending or has expired",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

        key = component_upload_object_key(
            tenant=upload["tenant"],
            provider=upload["provider"],
            component_kind=upload["component_kind"],
            upload_id=upload_id,
        )
        head = object_store.head(key)
        object_sha256 = _s3_checksum_to_component_sha256(
            None if head is None else head.checksum_sha256
        )
        if (
            head is None
            or head.size_bytes != upload["size_bytes"]
            or object_sha256 != upload["sha256"]
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
        component_id = _inserted_id(inserted)
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


def _inserted_id(row: tuple[UUID] | None) -> UUID:
    if row is None:
        raise CategorizedError(
            "provider component insert did not return an id",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    return row[0]


def _s3_checksum_to_component_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith("sha256:"):
        prefix, digest = value.split(":", 1)
        if len(digest) == 64 and all(char in "0123456789abcdefABCDEF" for char in digest):
            return f"{prefix}:{digest.lower()}"
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 32:
        return None
    return f"sha256:{decoded.hex()}"


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
        visibility=cast(Visibility, row["visibility"]),
        project=cast(str | None, row["project"]),
        principal=cast(str, row["principal"]),
        sha256=cast(str | None, row["sha256"]),
    )
