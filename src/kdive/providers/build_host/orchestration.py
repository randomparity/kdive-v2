"""Provider-neutral kernel build-host orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, CatalogConfigFetch
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.references import ComponentRef
from kdive.provider_components.requirements import validate_config_requirements
from kdive.providers.build_host.common import _dropped_fragment_symbols
from kdive.providers.build_host.config import (
    DEFAULT_BUILD_COMPONENT_ROOT,
    load_profile_config_requirements,
    missing_config_groups,
    resolve_config_bytes,
    validate_config_ref,
)
from kdive.providers.build_host.execution import ReadConfig, RunStep, build_failure
from kdive.providers.build_host.workspace import Checkout

REQUIRED_KERNEL_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)


@dataclass(slots=True)
class BuildHostOrchestrator:
    """Shared build-host config resolution, preflight, and ``make`` orchestration."""

    workspace_root: Path
    catalog_fetch: CatalogConfigFetch
    checkout: Checkout
    run_olddefconfig: RunStep
    read_config: ReadConfig
    run_make: RunStep
    allowed_component_roots: list[Path]

    @classmethod
    def create(
        cls,
        *,
        workspace_root: Path,
        catalog_fetch: CatalogConfigFetch,
        checkout: Checkout,
        run_olddefconfig: RunStep,
        read_config: ReadConfig,
        run_make: RunStep,
        allowed_component_roots: list[Path] | None = None,
    ) -> BuildHostOrchestrator:
        """Build an orchestrator with the default component-root allowlist."""
        return cls(
            workspace_root=workspace_root,
            catalog_fetch=catalog_fetch,
            checkout=checkout,
            run_olddefconfig=run_olddefconfig,
            read_config=read_config,
            run_make=run_make,
            allowed_component_roots=allowed_component_roots or [Path(DEFAULT_BUILD_COMPONENT_ROOT)],
        )

    def build_workspace(self, run_id: UUID, profile: ServerBuildProfile) -> Path:
        """Resolve config, checkout, preflight, run ``make``, and return the workspace path."""
        workspace = self.workspace_root / str(run_id)
        config_ref = profile.config or DEFAULT_CONFIG_REF
        fragment_bytes = resolve_config_bytes(
            config_ref,
            allowed_component_roots=self.allowed_component_roots,
            catalog_fetch=self.catalog_fetch,
        )
        fragment_text = fragment_bytes.decode()
        self.checkout(run_id, profile, workspace, fragment_bytes)
        if self.run_olddefconfig(workspace) != 0:
            raise build_failure("make olddefconfig exited non-zero", run_id)
        config_text = self.read_config(workspace)
        _validate_final_config(run_id, profile, fragment_text, config_text)
        if self.run_make(workspace) != 0:
            raise build_failure("make exited non-zero", run_id)
        return workspace

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation."""
        validate_config_ref(ref, allowed_component_roots=self.allowed_component_roots)


def _validate_final_config(
    run_id: UUID, profile: ServerBuildProfile, fragment_text: str, config_text: str
) -> None:
    dropped = _dropped_fragment_symbols(fragment_text, config_text)
    if dropped:
        raise CategorizedError(
            "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"dropped": dropped},
        )
    missing = missing_config_groups(config_text, REQUIRED_KERNEL_CONFIG)
    if missing:
        raise CategorizedError(
            "kernel .config omits a required kdump/debuginfo option",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"missing_any_of": [list(group) for group in missing]},
        )
    if profile.profile_requirements is not None:
        requirements = load_profile_config_requirements(
            provider=profile.profile_requirements.provider,
            name=profile.profile_requirements.name,
        )
        try:
            validate_config_requirements(config_text, requirements)
        except CategorizedError as exc:
            exc.details.setdefault("run_id", str(run_id))
            raise
