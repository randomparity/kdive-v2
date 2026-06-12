"""Remote-libvirt Build plane: worker ``make`` + a single vmlinuz+modules bundle (ADR-0081).

`RemoteLibvirtBuild` runs the kernel build on the **worker** exactly as `local_libvirt` does
— warm-tree checkout (rsync + staged ``.config`` + optional patch), ``make olddefconfig``, a
kdump/debuginfo ``.config`` preflight, ``make`` — then runs ``make modules_install`` and
publishes **one gzip-compressed install bundle** (`boot/vmlinuz` + `lib/modules/<ver>/…`) as
``kernel_ref`` plus the ``vmlinux`` debuginfo as ``debuginfo_ref``, recording the GNU
build-id. This leaves ``BuildOutput``, the ``Builder`` port, and the ``runs`` ledger
unchanged: the remote target is a disk-image base OS that installs the kernel **in-guest**
(ADR-0078), which needs the kernel's ``/lib/modules`` tree that local's direct-kernel boot
never required — so the modules travel inside the existing ``kernel_ref`` object rather than
as a third ref (which would need a port change or core DDL beyond migration 0020).

This module is **independent** of ``local_libvirt`` (ADR-0076: no shared layer with the
provider headed for removal); it reuses only the already-neutral ``provider_components`` /
``provider_components.build_validation`` helpers and duplicates the build mechanics. The slow,
environment-bound operations are **injected seams** that default to the real implementations,
so unit tests cover the orchestration/error contract without a toolchain; the real ``make``
path is exercised under the ``live_vm`` gate. `build()` is synchronous; the async build
handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import kdive.config as config
from kdive.build_configs.defaults import (
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.references import ComponentRef
from kdive.providers import build_host_config as _build_config
from kdive.providers import build_host_execution as _build_exec
from kdive.providers import build_host_workspace as _build_workspace
from kdive.providers.build_host_orchestration import BuildHostOrchestrator
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
# The back-reference symlinks make modules_install plants in /lib/modules/<ver>/; they point
# at absolute paths in the worker's build tree and must not enter the in-guest bundle.
_MODULE_BACKREF_LINKS = frozenset({"build", "source"})


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


type _BuildBundle = Callable[[Path, Path], bytes]
type _StagingFactory = Callable[[], Path]


class RemoteLibvirtBuild:
    """The realized remote Build port: worker ``make`` + one vmlinuz+modules bundle (ADR-0081)."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        store_factory: Callable[[], _StorePort],
        checkout: _build_workspace.Checkout,
        run_olddefconfig: _build_exec.RunStep,
        read_config: _build_exec.ReadConfig,
        run_make: _build_exec.RunStep,
        run_modules_install: _build_exec.RunModulesInstall,
        build_bundle: _BuildBundle,
        read_vmlinux: _build_exec.ReadBytes,
        read_build_id: _build_exec.ReadBuildId,
        staging_factory: _StagingFactory,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
    ) -> None:
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
        )
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._run_modules_install = run_modules_install
        self._build_bundle = build_bundle
        self._read_vmlinux = read_vmlinux
        self._read_build_id = read_build_id
        self._staging_factory = staging_factory

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtBuild:
        """Build from the shared ``KDIVE_*`` worker build env; does not spawn ``make`` or S3.

        Reads the worker's build-host config — the workspace root (``KDIVE_BUILD_WORKSPACE``),
        the warm source tree (``KDIVE_KERNEL_SRC``), and the component roots
        (``KDIVE_BUILD_COMPONENT_ROOTS``) — the same vars ``local_libvirt`` reads; they
        describe the worker, not the provider. The object store is built lazily from the
        ``KDIVE_S3_*`` env on the first ``build()``, and the seams default to the real
        subprocess/ELF implementations, which run only when ``build()`` is called.
        """
        workspace_root = Path(config.require(BUILD_WORKSPACE))
        kernel_src = config.require(KERNEL_SRC)
        allowed_component_roots = _build_config.build_component_roots_from_env()
        return cls(
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_build_workspace.make_checkout(kernel_src, secret_registry),
            run_olddefconfig=_build_exec.real_run_olddefconfig,
            read_config=_build_exec.real_read_config,
            run_make=_build_exec.real_run_make,
            run_modules_install=_build_exec.real_run_modules_install,
            build_bundle=_real_build_bundle,
            read_vmlinux=_build_exec.real_read_vmlinux,
            read_build_id=_build_exec.real_read_build_id,
            staging_factory=_real_staging_factory,
            catalog_fetch=build_config_fetch_from_env(),
            allowed_component_roots=allowed_component_roots,
        )

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel, publish a vmlinuz+modules bundle + debuginfo; return refs + build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE`` on a
                non-zero ``make``/``olddefconfig``/``modules_install`` exit or a missing
                build-id; ``INFRASTRUCTURE_FAILURE`` propagated from a failed artifact store.
        """
        workspace = self._orchestrator.build_workspace(run_id, profile)
        mod_root = self._staging_factory()
        try:
            if self._run_modules_install(workspace, mod_root) != 0:
                raise _build_exec.build_failure("make modules_install exited non-zero", run_id)
            build_id = self._read_build_id(workspace)
            bundle = self._build_bundle(workspace, mod_root)
        finally:
            shutil.rmtree(mod_root, ignore_errors=True)
        kernel = self._put(run_id, "kernel", bundle)
        vmlinux = self._put(run_id, "vmlinux", self._read_vmlinux(workspace))
        return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        self._orchestrator.validate_config_ref(ref)

    def _put(self, run_id: UUID, name: str, data: bytes) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            ArtifactWriteRequest(
                tenant=_TENANT,
                owner_kind="runs",
                owner_id=str(run_id),
                name=name,
                data=data,
                sensitivity=Sensitivity.SENSITIVE,
                retention_class=_RETENTION_CLASS,
            )
        )


def _build_bundle_member_dirs(modules_root: Path) -> list[Path]:
    """Sorted paths under ``modules_root``, dropping the absolute back-reference symlinks."""
    members: list[Path] = []
    for path in sorted(modules_root.rglob("*")):
        if path.is_symlink() and path.name in _MODULE_BACKREF_LINKS:
            continue
        members.append(path)
    return members


def _real_build_bundle(workspace: Path, mod_root: Path) -> bytes:
    """Package ``boot/vmlinuz`` + ``lib/modules/<ver>/…`` into one gzip-compressed tar (bytes).

    The bzImage is renamed to ``boot/vmlinuz`` and every real file under the staging tree's
    ``lib/modules`` is added under a ``lib/modules/…`` arcname; the ``build``/``source``
    back-reference symlinks ``make modules_install`` plants (absolute worker paths) are
    excluded so the in-guest extract carries no dangling links. The whole object is held in
    memory for the single PUT — the same whole-object model local already uses, kept small by
    gzip (ADR-0081).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # A zero-exit make can still leave no bzImage, and a module file can vanish mid-pack;
        # both must surface as a typed BUILD_FAILURE, not a bare OSError that escapes the
        # provider error contract (the local-libvirt parity guard).
        _add_bundle_member(tar, workspace / "arch/x86/boot/bzImage", "boot/vmlinuz", "bzImage")
        modules_root = mod_root / "lib" / "modules"
        for path in _build_bundle_member_dirs(modules_root):
            arcname = "lib/modules/" + str(path.relative_to(modules_root))
            _add_bundle_member(tar, path, arcname, "module bundle", recursive=False)
    return buf.getvalue()


def _add_bundle_member(
    tar: tarfile.TarFile, path: Path, arcname: str, output: str, *, recursive: bool = True
) -> None:
    try:
        tar.add(path, arcname=arcname, recursive=recursive)
    except OSError as exc:
        raise CategorizedError(
            "kernel bundle could not be packaged",
            category=ErrorCategory.BUILD_FAILURE,
            details={"output": output},
        ) from exc


def _real_staging_factory() -> Path:  # pragma: no cover - live_vm
    return Path(tempfile.mkdtemp(prefix="kdive-mod-"))


__all__ = ["RemoteLibvirtBuild"]
