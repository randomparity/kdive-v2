"""Local-libvirt Build plane: make a kernel and store two artifacts (ADR-0029/0101).

`LocalLibvirtBuild` checks out a kernel source tree (warm tree + the profile's optional patch,
or — over a transport — a git clone), preflights the resolved ``.config`` for the
kdump/debuginfo prerequisites, runs ``make``, extracts the produced ``vmlinux``'s GNU build-id,
and stores two ``sensitive`` artifacts under deterministic Run-keyed object keys — the bootable
kernel image (`kernel`, the raw ``bzImage``) and the ``vmlinux``/debuginfo (`vmlinux`). It
returns both object keys plus the build-id (:class:`BuildOutput`). A local System
direct-kernel-boots, so no ``/lib/modules`` bundle is produced (unlike remote-libvirt).

Each artifact is produced as an :class:`ArtifactSource`: the worker-local default reads the file
into memory (:class:`ArtifactBytes`, PUT directly), while :meth:`LocalLibvirtBuild.over_transport`
leaves it on the build host (:class:`ArtifactRemoteFile`, published via a presigned PUT whose
checksum is computed host-side, so the worker never reads the bytes — ADR-0101).

The slow, environment-bound operations are **injected seams** that default to the real
implementations, so unit tests cover the orchestration/error contract without a toolchain; the
real ``make`` path is exercised under the ``live_vm`` gate. `build()` is synchronous; the async
build handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import kdive.config as config
from kdive.build_configs.defaults import (
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.references import (
    ComponentRef,
)
from kdive.providers.build_host import config as _build_config
from kdive.providers.build_host import execution as _build_exec
from kdive.providers.build_host import workspace as _build_workspace
from kdive.providers.build_host.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
    StorePort,
    publish_artifact_source,
)
from kdive.providers.build_host.orchestration import BuildHostOrchestrator, WorkspaceCleanup
from kdive.providers.build_host.transport import BuildTransport
from kdive.providers.build_host.transport_seams import (
    transport_git_checkout,
    transport_read_build_id,
    transport_read_config,
    transport_run_make,
    transport_run_olddefconfig,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_RETENTION_CLASS = "build"


type _Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]
type _RunOlddefconfig = _build_exec.RunStep
type _RunMake = _build_exec.RunStep
type _ReadArtifactSource = Callable[[Path], ArtifactSource]


class LocalLibvirtBuild:
    """The realized Build port: ``make`` + two-artifact store (ADR-0029/0101 §5)."""

    def __init__(
        self,
        *,
        tenant: str,
        workspace_root: Path,
        store_factory: Callable[[], StorePort],
        checkout: _Checkout,
        run_olddefconfig: _RunOlddefconfig,
        read_config: _build_exec.ReadConfig,
        run_make: _RunMake,
        read_kernel_source: _ReadArtifactSource,
        read_vmlinux_source: _ReadArtifactSource,
        read_build_id: _build_exec.ReadBuildId,
        secret_registry: SecretRegistry,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
        workspace_cleanup: WorkspaceCleanup | None = None,
    ) -> None:
        self._tenant = tenant
        self._workspace_root = workspace_root
        self._allowed_component_roots = allowed_component_roots or [
            Path(_build_config.DEFAULT_BUILD_COMPONENT_ROOT)
        ]
        self._orchestrator = BuildHostOrchestrator.create(
            workspace_root=workspace_root,
            catalog_fetch=catalog_fetch,
            checkout=checkout,
            run_olddefconfig=run_olddefconfig,
            read_config=read_config,
            run_make=run_make,
            allowed_component_roots=self._allowed_component_roots,
            cleanup=workspace_cleanup,
        )
        self._store_factory = store_factory
        self._store: StorePort | None = None
        self._read_kernel_source = read_kernel_source
        self._read_vmlinux_source = read_vmlinux_source
        self._read_build_id = read_build_id
        self._secret_registry = secret_registry
        self._catalog_fetch = catalog_fetch

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
            read_kernel_source=_local_kernel_source,
            read_vmlinux_source=_local_vmlinux_source,
            read_build_id=_build_exec.real_read_build_id,
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
            secret_registry=secret_registry,
        )

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> LocalLibvirtBuild:
        """Return a sibling builder whose build runs ON ``transport``'s host (ADR-0101).

        Every build step — git checkout, ``olddefconfig``, ``.config`` read, ``make``, build-id —
        runs over ``transport`` on the build host, while the worker-side config/store of ``self``
        (the catalog fetch, object-store factory, tenant, and component-root allowlist) is reused
        so config-fragment resolution and the presigned publish stay worker-side. The bzImage and
        ``vmlinux`` are published from the host via presigned PUT. A local System
        direct-kernel-boots, so no module bundle is produced (unlike remote-libvirt).

        Args:
            transport: A ready :class:`BuildTransport` (e.g. an SSH transport with a live
                identity) that runs every build step on the build host.
            host_workspace_root: Absolute path on the build host under which the per-run clone is
                created.
            git_remote: Git remote to clone on the host.
            git_ref: Git ref (tag, branch, or commit SHA) to check out on the host.
            secret_registry: Registry passed to the git-checkout seam for error redaction.

        Returns:
            A new :class:`LocalLibvirtBuild` bound to ``transport``.
        """
        host_root = Path(host_workspace_root)
        return LocalLibvirtBuild(
            tenant=self._tenant,
            workspace_root=host_root,
            store_factory=self._store_factory,
            checkout=transport_git_checkout(transport, git_remote, git_ref, secret_registry),
            run_olddefconfig=transport_run_olddefconfig(transport),
            read_config=transport_read_config(transport),
            run_make=transport_run_make(transport),
            read_kernel_source=lambda ws: ArtifactRemoteFile(
                str(ws / "arch/x86/boot/bzImage"), transport
            ),
            read_vmlinux_source=lambda ws: ArtifactRemoteFile(str(ws / "vmlinux"), transport),
            read_build_id=transport_read_build_id(transport),
            secret_registry=secret_registry,
            catalog_fetch=self._catalog_fetch,
            allowed_component_roots=self._allowed_component_roots,
            workspace_cleanup=lambda ws: transport.cleanup(str(ws)),
        )

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store two artifacts; return their refs and the build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE``
                on a non-zero ``make`` exit or a missing build-id; ``INFRASTRUCTURE_FAILURE``
                propagated from a failed artifact store.
        """
        workspace = self._orchestrator.workspace_path(run_id)
        try:
            self._orchestrator.build_workspace(run_id, profile)
            build_id = self._read_build_id(workspace)
            kernel = self.publish(run_id, "kernel", self._read_kernel_source(workspace))
            vmlinux = self.publish(run_id, "vmlinux", self._read_vmlinux_source(workspace))
            return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)
        finally:
            self._orchestrator.cleanup_workspace(workspace)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        self._orchestrator.validate_config_ref(ref)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact; bytes PUT directly, host files via presigned PUT.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` propagated from a failed store
                operation or presigned upload; ``BUILD_FAILURE`` if the host-side hash/size of a
                remote file cannot be read.
        """
        if self._store is None:
            self._store = self._store_factory()
        return publish_artifact_source(
            self._store,
            run_id,
            name,
            source,
            tenant=self._tenant,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        )


def _local_kernel_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local kernel seam: read the ``bzImage`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_kernel_image(workspace))


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local vmlinux seam: read ``vmlinux`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))


__all__ = [
    "LocalLibvirtBuild",
]
