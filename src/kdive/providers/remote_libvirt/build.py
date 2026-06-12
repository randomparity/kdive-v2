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
    DEFAULT_CONFIG_REF,
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components import build_host as _build_host
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.build_host import (
    DEFAULT_BUILD_COMPONENT_ROOT as _DEFAULT_BUILD_COMPONENT_ROOT,
)
from kdive.provider_components.build_host import (
    Checkout as _Checkout,
)
from kdive.provider_components.build_host import (
    ReadBuildId as _ReadBuildId,
)
from kdive.provider_components.build_host import (
    ReadBytes as _ReadBytes,
)
from kdive.provider_components.build_host import (
    ReadConfig as _ReadConfig,
)
from kdive.provider_components.build_host import (
    RunModulesInstall as _RunModulesInstall,
)
from kdive.provider_components.build_host import (
    RunStep as _RunStep,
)
from kdive.provider_components.build_host import (
    build_component_roots_from_env as _build_component_roots_from_env,
)
from kdive.provider_components.build_host import (
    build_failure as _build_failure,
)
from kdive.provider_components.build_host import (
    make_checkout as _make_checkout,
)
from kdive.provider_components.build_host import (
    missing_config_groups,
    validate_config_ref,
)
from kdive.provider_components.build_host import (
    real_read_build_id as _real_read_build_id,
)
from kdive.provider_components.build_host import (
    real_read_config as _real_read_config,
)
from kdive.provider_components.build_host import (
    real_read_vmlinux as _real_read_vmlinux,
)
from kdive.provider_components.build_host import (
    real_run_make as _real_run_make,
)
from kdive.provider_components.build_host import (
    real_run_modules_install as _real_run_modules_install,
)
from kdive.provider_components.build_host import (
    real_run_olddefconfig as _real_run_olddefconfig,
)
from kdive.provider_components.build_host import (
    resolve_config_bytes as _resolve_config_bytes,
)
from kdive.provider_components.references import ComponentRef
from kdive.providers.build_common import _dropped_fragment_symbols
from kdive.providers.ports import BuildOutput
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
_MAKE_TIMEOUT_S = _build_host.MAKE_TIMEOUT_S
_OBJCOPY_TIMEOUT_S = _build_host.OBJCOPY_TIMEOUT_S
_GIT_APPLY_TIMEOUT_S = _build_host.GIT_APPLY_TIMEOUT_S
_RSYNC_TIMEOUT_S = _build_host.RSYNC_TIMEOUT_S
_apply_patch = _build_host.apply_patch
_real_checkout = _build_host.real_checkout
_resolve_local_ref = _build_host.resolve_local_ref
_sync_tree = _build_host.sync_tree
subprocess = _build_host.subprocess
# The back-reference symlinks make modules_install plants in /lib/modules/<ver>/; they point
# at absolute paths in the worker's build tree and must not enter the in-guest bundle.
_MODULE_BACKREF_LINKS = frozenset({"build", "source"})

# The kdump prerequisite is satisfied by CONFIG_CRASH_DUMP; symbolization needs DWARF or BTF
# debuginfo. Each tuple is an OR-group: the config must enable at least one of each.
_REQUIRED_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


type _BuildBundle = Callable[[Path, Path], bytes]
type _StagingFactory = Callable[[], Path]


def _missing_config_groups(config_text: str) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text`` (``CONFIG_X=y``)."""
    return missing_config_groups(config_text, _REQUIRED_CONFIG)


class RemoteLibvirtBuild:
    """The realized remote Build port: worker ``make`` + one vmlinuz+modules bundle (ADR-0081)."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        store_factory: Callable[[], _StorePort],
        checkout: _Checkout,
        run_olddefconfig: _RunStep,
        read_config: _ReadConfig,
        run_make: _RunStep,
        run_modules_install: _RunModulesInstall,
        build_bundle: _BuildBundle,
        read_vmlinux: _ReadBytes,
        read_build_id: _ReadBuildId,
        staging_factory: _StagingFactory,
        catalog_fetch: CatalogConfigFetch,
        allowed_component_roots: list[Path] | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._allowed_component_roots = allowed_component_roots or [
            Path(_DEFAULT_BUILD_COMPONENT_ROOT)
        ]
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._catalog_fetch = catalog_fetch
        self._checkout = checkout
        self._run_olddefconfig = run_olddefconfig
        self._read_config = read_config
        self._run_make = run_make
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
        allowed_component_roots = _build_component_roots_from_env()
        return cls(
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_make_checkout(kernel_src, secret_registry),
            run_olddefconfig=_real_run_olddefconfig,
            read_config=_real_read_config,
            run_make=_real_run_make,
            run_modules_install=_real_run_modules_install,
            build_bundle=_real_build_bundle,
            read_vmlinux=_real_read_vmlinux,
            read_build_id=_real_read_build_id,
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
        workspace = self._workspace_root / str(run_id)
        config_ref = profile.config or DEFAULT_CONFIG_REF
        fragment_bytes = _resolve_config_bytes(
            config_ref,
            allowed_component_roots=self._allowed_component_roots,
            catalog_fetch=self._catalog_fetch,
        )
        fragment_text = fragment_bytes.decode()
        self._checkout(run_id, profile, workspace, fragment_bytes)
        if self._run_olddefconfig(workspace) != 0:
            raise _build_failure("make olddefconfig exited non-zero", run_id)
        final_config = self._read_config(workspace)
        dropped = _dropped_fragment_symbols(fragment_text, final_config)
        if dropped:
            raise CategorizedError(
                "kdump fragment symbols were dropped by olddefconfig (unmet base dependency)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"dropped": dropped},
            )
        missing = _missing_config_groups(final_config)
        if missing:
            raise CategorizedError(
                "kernel .config omits a required kdump/debuginfo option",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"missing_any_of": [list(group) for group in missing]},
            )
        if self._run_make(workspace) != 0:
            raise _build_failure("make exited non-zero", run_id)
        mod_root = self._staging_factory()
        try:
            if self._run_modules_install(workspace, mod_root) != 0:
                raise _build_failure("make modules_install exited non-zero", run_id)
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
        validate_config_ref(ref, allowed_component_roots=self._allowed_component_roots)

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
