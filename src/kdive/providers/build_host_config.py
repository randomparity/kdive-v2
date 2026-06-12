"""Build-host component/config reference resolution helpers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import kdive.config as config
from kdive.build_configs.defaults import CatalogConfigFetch
from kdive.config.core_settings import BUILD_COMPONENT_ROOTS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.catalog import load_fixture_catalog
from kdive.provider_components.local_paths import validate_local_component_path
from kdive.provider_components.references import (
    CatalogComponentRef,
    ComponentRef,
    LocalComponentRef,
)
from kdive.provider_components.requirements import ConfigRequirements

DEFAULT_BUILD_COMPONENT_ROOT = "/var/lib/kdive/build/components"


def missing_config_groups(
    config_text: str, required_config: tuple[tuple[str, ...], ...]
) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text``."""
    enabled = {
        line.split("=", 1)[0]
        for line in config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith("=y")
    }
    return [group for group in required_config if not any(opt in enabled for opt in group)]


def load_profile_config_requirements(provider: str, name: str) -> ConfigRequirements:
    """Load the named fixture profile's config requirements."""
    profile = load_fixture_catalog().profile(provider, name)
    if profile is None:
        raise CategorizedError(
            "unknown build profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name},
        )
    return profile.requires.config


def build_component_roots_from_env() -> list[Path]:
    """Read the worker build component root allowlist from ``KDIVE_BUILD_COMPONENT_ROOTS``."""
    raw = config.get(BUILD_COMPONENT_ROOTS)
    if raw is None:
        return [Path(DEFAULT_BUILD_COMPONENT_ROOT)]
    return [Path(part) for part in raw.split(":") if part]


def ref_error(kind: str, message: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` for a bad ref; details name the field, not its value."""
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )


def resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref to an existing local file."""
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise ref_error(kind, "config/patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise ref_error(kind, "config/patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise ref_error(kind, "config/patch ref must be an absolute path")
    if not path.is_file():
        raise ref_error(kind, "config/patch ref does not resolve to a readable file")
    return path


def resolve_config_bytes(
    ref: ComponentRef,
    *,
    allowed_component_roots: list[Path],
    catalog_fetch: CatalogConfigFetch,
) -> bytes:
    """Resolve a config ref to fragment bytes."""
    if isinstance(ref, LocalComponentRef):
        path = validate_local_component_path(
            ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
        )
        return path.read_bytes()
    if isinstance(ref, CatalogComponentRef):
        return catalog_fetch(ref.name)
    raise ref_error("config", "config component ref must be local or catalog for builds")


def validate_config_ref(ref: ComponentRef, *, allowed_component_roots: list[Path]) -> None:
    """Validate a build config ref shape at run creation."""
    if isinstance(ref, LocalComponentRef):
        validate_local_component_path(
            ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
        )
        return
    if isinstance(ref, CatalogComponentRef):
        return
    raise ref_error("config", "config component ref must be local or catalog for builds")
