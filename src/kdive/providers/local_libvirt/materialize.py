"""Local-libvirt component materialization (ADR-0065)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from kdive.components.catalog import DEFAULT_FIXTURE_CATALOG_PATH, load_fixture_catalog
from kdive.components.local_paths import validate_local_component_path
from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    ComponentRef,
    LocalComponentRef,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


class ProviderComponent(Protocol):
    id: UUID
    source: ComponentRef


class ComponentStore(Protocol):
    def get_visible_component(
        self,
        component_id: UUID,
        *,
        project: str,
    ) -> ProviderComponent | None:
        """Return an authorized provider component or None."""


class FetchedArtifact(Protocol):
    path: Path
    sha256: str
    size_bytes: int


class ObjectStore(Protocol):
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        """Fetch an authorized object-store artifact."""


def materialize_rootfs_base(
    ref: ComponentRef,
    *,
    allowed_roots: list[Path],
    cache_dir: Path,
    project: str,
    component_store: ComponentStore | None,
    object_store: ObjectStore | None,
) -> Path:
    """Return a provider-readable rootfs base image path."""
    _ = (cache_dir, project)
    if isinstance(ref, LocalComponentRef):
        return validate_local_component_path(
            ref.path,
            allowed_roots=allowed_roots,
            sha256=ref.sha256,
        )
    if isinstance(ref, CatalogComponentRef):
        return _materialize_catalog_rootfs(ref, allowed_roots=allowed_roots)
    if isinstance(ref, ArtifactComponentRef) or component_store is None or object_store is None:
        raise CategorizedError(
            "artifact-backed rootfs materialization is not wired yet",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    raise CategorizedError(
        "unsupported rootfs component reference",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _materialize_catalog_rootfs(ref: CatalogComponentRef, *, allowed_roots: list[Path]) -> Path:
    catalog = load_fixture_catalog(DEFAULT_FIXTURE_CATALOG_PATH)
    entry = catalog.rootfs_entry(ref.provider, ref.name)
    if entry is None:
        raise CategorizedError(
            "unknown provider rootfs catalog entry",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": ref.provider, "name": ref.name},
        )
    if isinstance(entry.source, LocalComponentRef):
        return validate_local_component_path(
            entry.source.path,
            allowed_roots=allowed_roots,
            sha256=entry.source.sha256,
        )
    raise CategorizedError(
        "artifact-backed rootfs materialization is not wired yet",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )
