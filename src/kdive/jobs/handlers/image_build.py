"""Worker handler for the ``IMAGE_BUILD`` job: build -> validate -> publish (ADR-0092, #285).

An operator ``images build``/``publish`` enqueues an ``IMAGE_BUILD`` job; the worker runs this
handler. It drives the provider's :class:`RootfsBuildPlane` (the blocking, minutes-long
libguestfs build is offloaded via ``asyncio.to_thread`` so it never stalls the worker event
loop), validates the built image against the guest contract, then publishes it through the
row-first :func:`publish_image` two-write. A guest-contract validation failure raises a
``CategorizedError(CONFIGURATION_ERROR)``, which the worker turns into a dead-letter with that
named category (no half-published row: validation gates the publish).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from psycopg import AsyncConnection

from kdive.domain.models import Job, JobKind
from kdive.images.planes.base import RootfsBuildPlane, RootfsBuildSpec
from kdive.images.validation import DEFAULT_INSPECT, InspectSeam, validate_guest_contract
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ImageBuildPayload, load_payload
from kdive.services.images.publish import (
    ImageObjectStore,
    PublishRequest,
    publish_image,
)

type RootfsBuildPlaneResolver = Callable[[str], RootfsBuildPlane]


def _spec(payload: ImageBuildPayload) -> RootfsBuildSpec:
    return RootfsBuildSpec(
        provider=payload.provider,
        name=payload.name,
        arch=payload.arch,
        releasever=payload.releasever,
        packages=payload.packages,
        source_image_digest=payload.source_image_digest,
        capabilities=payload.capabilities,
    )


async def image_build_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    build_plane: RootfsBuildPlane | None = None,
    plane_resolver: RootfsBuildPlaneResolver | None = None,
    store: ImageObjectStore,
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> str:
    """Build, guest-contract-validate, and publish a catalog image; return its object key.

    Args:
        conn: The worker dispatch connection.
        job: The claimed ``IMAGE_BUILD`` job.
        build_plane: A single injected rootfs build plane, used by focused tests.
        plane_resolver: Runtime resolver for ``payload.provider`` in production assembly.
        store: The image object store.
        inspect: The libguestfs inspection seam threaded into the validator (tests inject a stub).

    Returns:
        The registered image's object key (the job ``result_ref``).

    Raises:
        CategorizedError: the build, guest-contract validation (``CONFIGURATION_ERROR`` naming
            the missing element), or publish fails — the worker dead-letters with the category.
    """
    payload = load_payload(job, ImageBuildPayload)
    if plane_resolver is not None:
        build_plane = plane_resolver(payload.provider)
    if build_plane is None:
        raise RuntimeError("IMAGE_BUILD handler has no rootfs build plane resolver")
    output = await asyncio.to_thread(build_plane.build, _spec(payload))
    await asyncio.to_thread(
        validate_guest_contract,
        output.qcow2_path,
        required=list(payload.capabilities),
        inspect=inspect,
    )
    request = PublishRequest(
        provider=payload.provider,
        name=payload.name,
        arch=payload.arch,
        format=payload.format,
        root_device=payload.root_device,
        digest=output.digest,
        capabilities=payload.capabilities,
        provenance=output.provenance,
        visibility=payload.visibility,
        owner=payload.owner,
        expires_at=payload.expires_at,
    )
    entry = await publish_image(conn, store, request=request, source=output.qcow2_path)
    if entry.object_key is None:  # Invariant: a registered row always carries its object key.
        raise RuntimeError(f"published image {entry.id} has no object_key")
    return entry.object_key


def register_handlers(
    registry: HandlerRegistry,
    *,
    build_plane: RootfsBuildPlane | None = None,
    plane_resolver: RootfsBuildPlaneResolver | None = None,
    store: ImageObjectStore,
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> None:
    """Bind the ``IMAGE_BUILD`` job handler."""
    registry.register(
        JobKind.IMAGE_BUILD,
        lambda conn, job: image_build_handler(
            conn,
            job,
            build_plane=build_plane,
            plane_resolver=plane_resolver,
            store=store,
            inspect=inspect,
        ),
    )
