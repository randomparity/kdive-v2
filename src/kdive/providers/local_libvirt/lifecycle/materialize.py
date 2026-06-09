"""Local-libvirt component materialization (ADR-0065)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import RootfsSource, _UploadRootfs
from kdive.provider_components.catalog import load_fixture_catalog
from kdive.provider_components.local_paths import validate_local_component_path
from kdive.provider_components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    ComponentRef,
    ComponentUploadRef,
    LocalComponentRef,
)


@dataclass(frozen=True, slots=True)
class RootfsUploadContext:
    """System-owned upload staging context for an uploaded rootfs."""

    tenant: str
    system_id: UUID
    upload_dir: Path


@dataclass(frozen=True, slots=True)
class RootfsMaterializationContext:
    """Inputs needed to resolve a provider-readable rootfs base path."""

    allowed_roots: list[Path]
    upload: RootfsUploadContext | None = None


def materialize_rootfs_base(
    ref: RootfsSource | ComponentRef,
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
    if isinstance(ref, ArtifactComponentRef | ComponentUploadRef):
        raise CategorizedError(
            "artifact-backed rootfs materialization is not wired yet",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
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
    catalog = load_fixture_catalog()
    entry = catalog.rootfs_entry(ref.provider, ref.name)
    if entry is None:
        raise CategorizedError(
            "unknown provider rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": ref.provider, "name": ref.name},
        )
    if isinstance(entry.source, LocalComponentRef):
        return _materialize_local_rootfs(entry.source, context)
    raise CategorizedError(
        "artifact-backed rootfs materialization is not wired yet",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )
