"""Local-libvirt component materialization (ADR-0065, ADR-0092).

A ``catalog`` rootfs reference resolves through the DB-backed ``image_catalog`` and its object is
fetched to a checksum-verified local cache (the cutover from the read-only YAML lookup). The
resolve+fetch capability is injected as ``RootfsMaterializationContext.catalog_fetch`` because
the provider provision seam is synchronous and owns no Postgres connection; the worker wires a
concrete fetch (a connection + object store) into the context. The ``local`` and ``upload`` paths
are unchanged provider-local resolutions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import _UploadRootfs
from kdive.provider_components.local_paths import validate_local_component_path
from kdive.provider_components.references import (
    CatalogComponentRef,
    LocalComponentRef,
)

# Resolve a `catalog` reference to a provider-readable local path (DB row → object → cache).
type CatalogFetch = Callable[[CatalogComponentRef], Path]
type MaterializableRootfsRef = LocalComponentRef | CatalogComponentRef | _UploadRootfs


@dataclass(frozen=True, slots=True)
class RootfsUploadContext:
    """System-owned upload staging context for an uploaded rootfs."""

    tenant: str
    system_id: UUID
    upload_dir: Path


@dataclass(frozen=True, slots=True)
class RootfsMaterializationContext:
    """Inputs needed to resolve a provider-readable rootfs base path.

    ``catalog_fetch`` resolves a ``catalog`` reference through ``image_catalog`` and downloads its
    object to a checksum-verified cache; it is ``None`` in lanes that never resolve a catalog
    reference (then a ``catalog`` reference is a configuration error).
    """

    allowed_roots: list[Path]
    upload: RootfsUploadContext | None = None
    catalog_fetch: CatalogFetch | None = None


def materialize_rootfs_base(
    ref: MaterializableRootfsRef,
    *,
    context: RootfsMaterializationContext,
) -> Path:
    """Return a provider-readable rootfs base image path."""
    if isinstance(ref, _UploadRootfs):
        return _materialize_uploaded_rootfs(context)
    if isinstance(ref, LocalComponentRef):
        return _materialize_local_rootfs(ref, context)
    if isinstance(ref, CatalogComponentRef):
        return _materialize_catalog_rootfs(ref, context)
    raise CategorizedError(
        "unsupported rootfs component reference",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def upload_rootfs_path(tenant: str, system_id: UUID, *, upload_dir: Path) -> Path:
    """Return the local staging path for a System-owned uploaded rootfs object."""
    return upload_dir / f"{tenant}-systems-{system_id}-rootfs.qcow2"


def _materialize_uploaded_rootfs(context: RootfsMaterializationContext) -> Path:
    if context.upload is None:
        raise CategorizedError(
            "uploaded rootfs materialization requires upload context",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    upload = context.upload
    return upload_rootfs_path(upload.tenant, upload.system_id, upload_dir=upload.upload_dir)


def _materialize_local_rootfs(
    ref: LocalComponentRef, context: RootfsMaterializationContext
) -> Path:
    return validate_local_component_path(
        ref.path,
        allowed_roots=context.allowed_roots,
        sha256=ref.sha256,
    )


def _materialize_catalog_rootfs(
    ref: CatalogComponentRef, context: RootfsMaterializationContext
) -> Path:
    """Resolve a ``catalog`` rootfs through the DB catalog and fetch its object to a cache.

    The resolve+fetch is injected (``context.catalog_fetch``) so the synchronous provider seam
    stays connectionless; an unwired lane treats a ``catalog`` reference as a configuration error.
    """
    if context.catalog_fetch is None:
        raise CategorizedError(
            "catalog rootfs materialization is not wired for this lane",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": ref.provider, "name": ref.name},
        )
    return context.catalog_fetch(ref)
