"""Project-private upload registration (ADR-0093, issue #286).

A developer uploads a custom rootfs through the ADR-0048 ingest, which lands the bytes as a
*quarantined* object (its guest contract unverified, its scope not yet owner-bound).
``register_private_upload`` turns that quarantined object into a bootable project-private catalog
image. It runs the whole sequence under the project advisory lock so the per-project count/bytes
quota is enforced fail-closed against concurrent uploads:

1. Read the quarantined object's bytes (its size and content digest).
2. Enforce the per-project quota fail-closed — a denial is audited and raises before any write.
3. Validate the image's guest contract; a non-conforming image is rejected *while still
   quarantined* (never registered, never promoted out of the quarantine prefix).
4. Delegate to :func:`publish_image` with ``visibility='private'``, ``owner=project``, and the
   (ceiling-clamped) ``expires_at`` — the single row-first publish path, no second implementation.

The owner of the registered image is the **project**; the uploading ``principal`` is recorded only
for audit attribution.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from psycopg import AsyncConnection
from psycopg.rows import dict_row

import kdive.config as config
from kdive.config.core_settings import (
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_BYTES,
    IMAGE_PRIVATE_MAX_COUNT,
)
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.images.validation import DEFAULT_INSPECT, InspectSeam, validate_guest_contract
from kdive.provider_components import artifacts as artifact_types
from kdive.security import audit
from kdive.services.images.publish import (
    ImageObjectStore,
    PublishRequest,
    publish_image,
)

_UPLOAD_TOOL = "images.upload"
_OBJECT_KIND = "image_catalog"
_QCOW2_FORMAT = "qcow2"
_ROOT_DEVICE = "/dev/vda"

# A project's live private images are the ones that occupy quota: a registered row, or a publish
# still in flight (`pending`). A `defined` baseline is public/object-less, so it never counts.
_LIVE_PRIVATE_STATES = (ImageState.PENDING.value, ImageState.REGISTERED.value)


class UploadObjectStore(ImageObjectStore, Protocol):
    """The object-store capability the upload path needs: publish's write/HEAD plus a read.

    Extends :class:`~kdive.services.images.publish.ImageObjectStore` (``put_artifact``/``head``)
    with the ``get_artifact`` the upload path uses to read the quarantined bytes. The concrete
    :class:`~kdive.store.objectstore.ObjectStore` satisfies it.
    """

    def get_artifact(self, key: str, etag: str | None) -> artifact_types.FetchedArtifact: ...


def _clamp_expiry(expires_at: datetime, *, now: datetime) -> datetime:
    """Clamp ``expires_at`` to the per-image lifetime ceiling (fail-closed on an over-long TTL)."""
    max_seconds = config.require(IMAGE_PRIVATE_LIFETIME_MAX)
    ceiling = now + timedelta(seconds=max_seconds)
    return min(expires_at, ceiling)


async def _project_usage(
    conn: AsyncConnection, project: str, store: UploadObjectStore
) -> tuple[int, int]:
    """Return the project's live private image count and total bytes, under the held lock.

    Counts ``pending`` + ``registered`` private rows owned by ``project`` and sums the size of
    their objects (HEAD each ``object_key``). A row whose object has gone missing contributes 0
    bytes but still counts toward the count cap. Runs under the held PROJECT lock, so the
    count-then-publish is atomic against a concurrent upload.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT object_key FROM image_catalog "
            "WHERE visibility = %(private)s AND owner = %(owner)s "
            "AND state = ANY(%(states)s)",
            {
                "private": ImageVisibility.PRIVATE.value,
                "owner": project,
                "states": list(_LIVE_PRIVATE_STATES),
            },
        )
        rows = await cur.fetchall()
    total_bytes = 0
    for row in rows:
        object_key = row["object_key"]
        if object_key is None:
            continue
        head = await asyncio.to_thread(store.head, object_key)
        if head is not None:
            total_bytes += head.size_bytes
    return len(rows), total_bytes


def _quota_denial(
    *, project: str, count: int, used_bytes: int, new_bytes: int
) -> CategorizedError | None:
    """Return the fail-closed quota denial for this upload, or ``None`` if it fits.

    Pure decision: the count cap admits one more row; the bytes cap admits ``new_bytes`` on top of
    the current total. No durable write — the caller audits the denial on a committed connection.
    """
    max_count = config.require(IMAGE_PRIVATE_MAX_COUNT)
    max_bytes = config.require(IMAGE_PRIVATE_MAX_BYTES)
    if count + 1 > max_count:
        return CategorizedError(
            f"project {project!r} is at its private-image count cap",
            category=ErrorCategory.QUOTA_EXCEEDED,
            details={"used": count, "cap": max_count},
        )
    if used_bytes + new_bytes > max_bytes:
        return CategorizedError(
            f"project {project!r} would exceed its private-image bytes cap",
            category=ErrorCategory.QUOTA_EXCEEDED,
            details={"used_bytes": used_bytes, "new_bytes": new_bytes, "cap_bytes": max_bytes},
        )
    return None


async def _audit_denial(
    conn: AsyncConnection, *, project: str, principal: str, name: str, denial: CategorizedError
) -> None:
    """Append the fail-closed quota-denial audit row on its own committed connection.

    Object-agnostic (no image was created), so it reuses :func:`audit.record_denial`'s reserved
    bare ``denied`` transition. Runs in its own transaction so the denial is durably audited even
    though the locked transaction that detected it rolled back without a write.
    """
    async with conn.transaction():
        await audit.record_denial(
            conn,
            event=audit.DenialEvent(
                principal=principal,
                agent_session=None,
                project=project,
                tool=_UPLOAD_TOOL,
                args={"provider": "local-libvirt", "name": name, "visibility": "private"},
                reason=str(denial),
            ),
        )


def _validate_staged(source: Path, required: Sequence[str], inspect: InspectSeam) -> None:
    """Guest-contract-validate the staged qcow2 (sync; the caller offloads via to_thread)."""
    validate_guest_contract(source, required=required, inspect=inspect)


async def register_private_upload(
    conn: AsyncConnection,
    store: UploadObjectStore,
    *,
    project: str,
    principal: str,
    name: str,
    provider: str,
    arch: str,
    quarantine_key: str,
    expires_at: datetime,
    required: Sequence[str],
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> ImageCatalogEntry:
    """Register a quarantined upload as a project-private catalog image under the project lock.

    Holds the PROJECT advisory lock across quota-check → validate → publish so the per-project
    count/bytes quota is enforced fail-closed: two concurrent uploads cannot both pass the cap.
    The quarantined object is validated against the guest contract *before* any publish write, so
    a non-conforming image is rejected while still quarantined (never registered). Delegates the
    durable write to :func:`publish_image` (``visibility='private'``, ``owner=project``); the
    uploading ``principal`` is recorded only for audit attribution.

    Args:
        conn: An async Postgres connection (autocommit; this function opens its own transaction
            to hold the project lock).
        store: The object store holding the quarantined object and receiving the published image.
        project: The owning project — the registered image resolves only within it.
        principal: The uploading principal, recorded for audit (not the image owner).
        name: The catalog image name.
        provider: The provider key the image targets (e.g. ``local-libvirt``).
        arch: The target architecture.
        quarantine_key: The object-store key of the quarantined upload.
        expires_at: The requested TTL deadline; clamped to the per-image lifetime ceiling.
        required: The guest-contract element tags the image must satisfy.
        inspect: The libguestfs inspection seam (defaults to a real ``guestfish`` probe; tests
            inject a stub).

    Returns:
        The persisted ``registered`` project-private :class:`ImageCatalogEntry`.

    Raises:
        CategorizedError: ``QUOTA_EXCEEDED`` (audited) if a cap would be breached;
            ``CONFIGURATION_ERROR`` if the image fails its guest contract or its bytes do not hash
            to the computed digest; ``STALE_HANDLE``/``INFRASTRUCTURE_FAILURE`` from the store.
    """
    _ = ImageVisibility.PRIVATE  # the registered scope is fixed for this path

    fetched = await asyncio.to_thread(store.get_artifact, quarantine_key, None)
    data = fetched.data
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    now = datetime.now(UTC)
    clamped_expiry = _clamp_expiry(expires_at, now=now)

    request = PublishRequest(
        provider=provider,
        name=name,
        arch=arch,
        format=_QCOW2_FORMAT,
        root_device=_ROOT_DEVICE,
        digest=digest,
        capabilities=tuple(required),
        provenance={"upload": {"principal": principal, "quarantine_key": quarantine_key}},
        visibility=ImageVisibility.PRIVATE.value,
        owner=project,
        expires_at=clamped_expiry,
    )

    with tempfile.TemporaryDirectory(prefix="kdive-upload-") as workdir:
        source = Path(workdir) / f"{arch}.qcow2"
        await asyncio.to_thread(source.write_bytes, data)
        await asyncio.to_thread(_validate_staged, source, required, inspect)

        entry = await _publish_under_quota(
            conn, store, request=request, source=source, principal=principal, new_bytes=len(data)
        )
    return entry


async def _publish_under_quota(
    conn: AsyncConnection,
    store: UploadObjectStore,
    *,
    request: PublishRequest,
    source: Path,
    principal: str,
    new_bytes: int,
) -> ImageCatalogEntry:
    """Hold the PROJECT lock, enforce the quota fail-closed, then publish; audit either outcome.

    A denial rolls back the locked transaction (no row written) and is audited durably on a
    separate transaction before raising, so an over-cap upload is denied **and** audited. The
    lock is held across the count/bytes read and the publish, so two concurrent uploads cannot
    both pass the cap.
    """
    project = request.owner
    if project is None:  # Invariant: this path always sets owner to the project.
        raise RuntimeError("private upload has no owning project")
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, project):
        count, used_bytes = await _project_usage(conn, project, store)
        denial = _quota_denial(
            project=project, count=count, used_bytes=used_bytes, new_bytes=new_bytes
        )
        if denial is None:
            entry = await publish_image(conn, store, request=request, source=source)
            await _audit_registration(conn, entry, principal=principal)
            return entry
    # Denied: the read-only locked transaction has closed (releasing the lock and writing
    # nothing). Audit the denial durably on a fresh transaction, then raise the typed error so
    # the over-cap upload is both denied and audited.
    await _audit_denial(
        conn, project=project, principal=principal, name=request.name, denial=denial
    )
    raise denial


async def _audit_registration(
    conn: AsyncConnection, entry: ImageCatalogEntry, *, principal: str
) -> None:
    """Append the success audit row attributing the upload to ``principal`` under the project."""
    if entry.owner is None:  # Invariant: a private image always carries its owning project.
        raise RuntimeError("registered private image has no owner project to audit under")
    await audit.record_system(
        conn,
        principal=principal,
        event=audit.AuditEvent(
            tool=_UPLOAD_TOOL,
            object_kind=_OBJECT_KIND,
            object_id=entry.id,
            transition="private-upload:registered",
            args={"provider": entry.provider, "name": entry.name, "arch": entry.arch},
            project=entry.owner,
        ),
    )
