"""Local-libvirt Build plane: make a kernel in a warm workspace and store two artifacts (ADR-0029).

`LocalLibvirtBuild` checks out a warm source tree (base ref + the profile's optional
patch), preflights the resolved ``.config`` for the kdump/debuginfo prerequisites, runs
``make`` incrementally, extracts the produced ``vmlinux``'s GNU build-id, and stores two
``sensitive`` artifacts under deterministic Run-keyed object keys — the bootable kernel
image (`kernel`) and the ``vmlinux``/debuginfo (`vmlinux`). It returns both object keys
plus the build-id (:class:`BuildOutput`).

The slow, environment-bound operations (warm-tree checkout, ``.config`` read, ``make``,
ELF reads, build-id extraction) are **injected seams** that default to the real
implementations, so unit tests cover the orchestration/error contract without a
toolchain; the real ``make`` path is exercised under the ``live_vm`` gate. `build()` is
synchronous; the async build handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import kdive.config as config
from kdive.build_configs.defaults import (
    DEFAULT_CONFIG_REF,
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.references import (
    ComponentRef,
)
from kdive.provider_components.requirements import validate_config_requirements
from kdive.providers import build_host_config as _build_config
from kdive.providers import build_host_execution as _build_exec
from kdive.providers import build_host_workspace as _build_workspace
from kdive.providers.build_common import _dropped_fragment_symbols
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_RETENTION_CLASS = "build"

# The kdump prerequisite is satisfied by CONFIG_CRASH_DUMP; symbolization needs DWARF or
# BTF debuginfo. Each tuple is an OR-group: the config must enable at least one of each.
_REQUIRED_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


def _missing_config_groups(config_text: str) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text`` (``CONFIG_X=y``)."""
    return _build_config.missing_config_groups(config_text, _REQUIRED_CONFIG)


type _Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]
type _RunOlddefconfig = _build_exec.RunStep
type _RunMake = _build_exec.RunStep


class LocalLibvirtBuild:
    """The realized Build port: warm-tree ``make`` + two-artifact store (ADR-0029 §5)."""

    def __init__(
        self,
        *,
        tenant: str,
        workspace_root: Path,
        store_factory: Callable[[], _StorePort],
        checkout: _Checkout,
        run_olddefconfig: _RunOlddefconfig,
        read_config: _build_exec.ReadConfig,
        run_make: _RunMake,
        read_kernel_image: _build_exec.ReadBytes,
        read_vmlinux: _build_exec.ReadBytes,
        read_build_id: _build_exec.ReadBuildId,
        secret_registry: SecretRegistry,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
    ) -> None:
        self._tenant = tenant
        self._workspace_root = workspace_root
        self._allowed_component_roots = allowed_component_roots or [
            Path(_build_config.DEFAULT_BUILD_COMPONENT_ROOT)
        ]
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._catalog_fetch = catalog_fetch
        self._checkout = checkout
        self._run_olddefconfig = run_olddefconfig
        self._read_config = read_config
        self._run_make = run_make
        self._read_kernel_image = read_kernel_image
        self._read_vmlinux = read_vmlinux
        self._read_build_id = read_build_id
        self._secret_registry = secret_registry

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> LocalLibvirtBuild:
        """Build from the ``KDIVE_*`` environment; does not spawn ``make`` or connect S3.

        Reads the workspace root (``KDIVE_BUILD_WORKSPACE``) and the warm source tree
        (``KDIVE_KERNEL_SRC``). The object store is built lazily from the ``KDIVE_S3_*``
        env on the first ``build()``, so the worker registers its handler without S3 env
        present. The seams default to the real subprocess/ELF implementations, which run
        only when ``build()`` is called.
        """
        workspace_root = Path(config.require(BUILD_WORKSPACE))
        kernel_src = config.require(KERNEL_SRC)
        allowed_component_roots = _build_config.build_component_roots_from_env()
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(kernel_src, secret_registry),
            run_olddefconfig=_build_exec.real_run_olddefconfig,
            read_config=_build_exec.real_read_config,
            run_make=_build_exec.real_run_make,
            read_kernel_image=_build_exec.real_read_kernel_image,
            read_vmlinux=_build_exec.real_read_vmlinux,
            read_build_id=_build_exec.real_read_build_id,
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
            secret_registry=secret_registry,
        )

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store two artifacts; return their refs and the build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE``
                on a non-zero ``make`` exit or a missing build-id; ``INFRASTRUCTURE_FAILURE``
                propagated from a failed artifact store.
        """
        workspace = self._workspace_root / str(run_id)
        config_ref = profile.config or DEFAULT_CONFIG_REF
        fragment_bytes = _build_config.resolve_config_bytes(
            config_ref,
            allowed_component_roots=self._allowed_component_roots,
            catalog_fetch=self._catalog_fetch,
        )
        fragment_text = fragment_bytes.decode()
        self._checkout(run_id, profile, workspace, fragment_bytes)
        if self._run_olddefconfig(workspace) != 0:
            raise CategorizedError(
                "make olddefconfig exited non-zero",
                category=ErrorCategory.BUILD_FAILURE,
                details={"run_id": str(run_id)},
            )
        config_text = self._read_config(workspace)
        dropped = _dropped_fragment_symbols(fragment_text, config_text)
        if dropped:
            raise CategorizedError(
                "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"dropped": dropped},
            )
        missing = _missing_config_groups(config_text)
        if missing:
            raise CategorizedError(
                "kernel .config omits a required kdump/debuginfo option",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"missing_any_of": [list(group) for group in missing]},
            )
        if profile.profile_requirements is not None:
            requirements = _build_config.load_profile_config_requirements(
                provider=profile.profile_requirements.provider,
                name=profile.profile_requirements.name,
            )
            validate_config_requirements(config_text, requirements)
        if self._run_make(workspace) != 0:
            raise CategorizedError(
                "make exited non-zero",
                category=ErrorCategory.BUILD_FAILURE,
                details={"run_id": str(run_id)},
            )
        build_id = self._read_build_id(workspace)
        kernel = self._put(run_id, "kernel", self._read_kernel_image(workspace))
        vmlinux = self._put(run_id, "vmlinux", self._read_vmlinux(workspace))
        return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        _build_config.validate_config_ref(
            ref, allowed_component_roots=self._allowed_component_roots
        )

    def _put(self, run_id: UUID, name: str, data: bytes) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=self._tenant,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                data=data,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
            )
        )


__all__ = [
    "LocalLibvirtBuild",
]
