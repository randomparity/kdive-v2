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

The post-``make`` pipeline (modules_install → build-id → bundle → vmlinux → publish) runs
through **injected seams** that produce an :class:`ArtifactSource`. The worker-local default
packages the bundle in memory and publishes via :meth:`ObjectStore.put_artifact` — byte-for-byte
the historical behavior. The transport-backed seams (ADR-0342) produce the artifacts on a
build host and publish each via a presigned PUT whose checksum is computed on the host, so the
worker never reads the large bundle/vmlinux bytes (it only sees the host-computed sha256).

This module is **independent** of ``local_libvirt`` (ADR-0076: no shared layer with the
provider headed for removal); it reuses only the already-neutral ``provider_components`` /
``provider_components.build_validation`` helpers and duplicates the build mechanics. The slow,
environment-bound operations are **injected seams** that default to the real implementations,
so unit tests cover the orchestration/error contract without a toolchain; the real ``make``
path is exercised under the ``live_vm`` gate. `build()` is synchronous; the async build
handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import base64
import io
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import kdive.config as config
from kdive.build_configs.defaults import (
    CatalogConfigFetch,
    build_config_fetch_from_env,
)
from kdive.config.core_settings import BUILD_WORKSPACE, KERNEL_SRC, UPLOAD_TTL_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
    artifact_key,
)
from kdive.provider_components.build_results import BuildOutput
from kdive.provider_components.references import ComponentRef
from kdive.providers.build_host import config as _build_config
from kdive.providers.build_host import execution as _build_exec
from kdive.providers.build_host import workspace as _build_workspace
from kdive.providers.build_host.orchestration import BuildHostOrchestrator
from kdive.providers.build_host.transport import BuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
_SENSITIVITY = Sensitivity.SENSITIVE
_MAX_PRESIGN_TTL_S = 3600
# The back-reference symlinks make modules_install plants in /lib/modules/<ver>/; they point
# at absolute paths in the worker's build tree and must not enter the in-guest bundle.
_MODULE_BACKREF_LINKS = frozenset({"build", "source"})


@dataclass(slots=True, frozen=True)
class ArtifactBytes:
    """An artifact the worker holds in memory and publishes with a direct PUT."""

    data: bytes


@dataclass(slots=True, frozen=True)
class ArtifactRemoteFile:
    """An artifact that lives on a build host and publishes via a presigned PUT.

    Attributes:
        path: Absolute path to the file on the build host.
        transport: The transport that can hash and upload that file.
    """

    path: str
    transport: BuildTransport


type ArtifactSource = ArtifactBytes | ArtifactRemoteFile


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...


type _MakeBundle = Callable[[Path, Path], ArtifactSource]
type _ReadVmlinuxSource = Callable[[Path], ArtifactSource]
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
        make_bundle: _MakeBundle,
        read_vmlinux_source: _ReadVmlinuxSource,
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
        self._make_bundle = make_bundle
        self._read_vmlinux_source = read_vmlinux_source
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
            make_bundle=_local_make_bundle,
            read_vmlinux_source=_local_vmlinux_source,
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
            kernel_source = self._make_bundle(workspace, mod_root)
            vmlinux_source = self._read_vmlinux_source(workspace)
            kernel = self.publish(run_id, "kernel", kernel_source)
            vmlinux = self.publish(run_id, "vmlinux", vmlinux_source)
        finally:
            shutil.rmtree(mod_root, ignore_errors=True)
        return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)

    def validate_config_ref(self, ref: ComponentRef) -> None:
        """Validate a build config ref's shape at run-creation (local path or catalog kind).

        A ``local`` ref is resolved against the provider roots; a ``catalog`` ref is accepted by
        kind (its existence is checked when the build fetches it, since this seam owns no DB
        connection). Any other kind is a ``CONFIGURATION_ERROR``.
        """
        self._orchestrator.validate_config_ref(ref)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact under ``runs/<run_id>/<name>`` and return its row.

        An :class:`ArtifactBytes` source is PUT directly from worker memory (the historical
        path). An :class:`ArtifactRemoteFile` source is published via a presigned PUT whose
        checksum is computed on the build host, so the worker never reads the file's bytes.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` propagated from a failed store
                operation or presigned upload.
        """
        store = self._store_for_publish()
        match source:
            case ArtifactBytes(data=data):
                return store.put_artifact(
                    ArtifactWriteRequest(
                        tenant=_TENANT,
                        owner_kind="runs",
                        owner_id=str(run_id),
                        name=name,
                        data=data,
                        sensitivity=_SENSITIVITY,
                        retention_class=_RETENTION_CLASS,
                    )
                )
            case ArtifactRemoteFile(path=path, transport=transport):
                return _publish_remote_file(store, run_id, name, path, transport)

    def _store_for_publish(self) -> _StorePort:
        if self._store is None:
            self._store = self._store_factory()
        return self._store


def _publish_remote_file(
    store: _StorePort, run_id: UUID, name: str, path: str, transport: BuildTransport
) -> StoredArtifact:
    """Presign a PUT for a host-resident file and have the transport upload it.

    The sha256 and byte size are read off the build host; ``presign_put`` signs the base64
    sha256 into the URL so S3 rejects a body that does not hash to it. The worker only ever
    handles the host-computed digest, never the file's bytes.
    """
    sha256_b64 = _remote_sha256_b64(transport, path)
    size_bytes = _remote_size_bytes(transport, path)
    key = artifact_key(_TENANT, "runs", str(run_id), name)
    presigned = store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=sha256_b64,
            size_bytes=size_bytes,
            sensitivity=_SENSITIVITY,
            retention_class=_RETENTION_CLASS,
            expires_in=_presign_ttl_seconds(),
        )
    )
    etag = transport.upload_file(path, presigned)
    return StoredArtifact(key, etag, _SENSITIVITY, _RETENTION_CLASS)


def _remote_sha256_b64(transport: BuildTransport, path: str) -> str:
    """Hash ``path`` on the build host with ``sha256sum`` and return the base64 digest.

    ``sha256sum`` prints ``"<hex>  <path>"``; the hex is parsed off the first field and
    converted to base64 of the raw 32-byte digest — the form ``presign_put`` signs into the
    ``x-amz-checksum-sha256`` header.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if ``sha256sum`` exits non-zero or prints output
            that does not parse to a 32-byte hex digest.
    """
    result = transport.run(["sha256sum", path], cwd=str(Path(path).parent), timeout_s=300)
    if result.returncode != 0:
        raise CategorizedError(
            "sha256sum of a build artifact exited non-zero",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "stderr": result.stderr[-512:]},
        )
    hex_digest = result.stdout.split()[0] if result.stdout.split() else ""
    try:
        raw = bytes.fromhex(hex_digest)
    except ValueError as exc:
        raise CategorizedError(
            "sha256sum produced an unparseable digest",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path},
        ) from exc
    if len(raw) != 32:
        raise CategorizedError(
            "sha256sum produced a digest of the wrong length",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "len": len(raw)},
        )
    return base64.b64encode(raw).decode("ascii")


def _remote_size_bytes(transport: BuildTransport, path: str) -> int:
    """Return the byte size of ``path`` on the build host via ``stat -c %s``.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if ``stat`` exits non-zero or prints a
            non-integer size.
    """
    result = transport.run(["stat", "-c", "%s", path], cwd=str(Path(path).parent), timeout_s=60)
    if result.returncode != 0:
        raise CategorizedError(
            "stat of a build artifact exited non-zero",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path, "stderr": result.stderr[-512:]},
        )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise CategorizedError(
            "stat produced a non-integer size",
            category=ErrorCategory.BUILD_FAILURE,
            details={"path": path},
        ) from exc


def _presign_ttl_seconds() -> int:
    """The presigned-PUT lifetime: ``KDIVE_UPLOAD_TTL_SECONDS`` capped at the S3 max of 3600."""
    return min(_MAX_PRESIGN_TTL_S, config.require(UPLOAD_TTL_SECONDS))


def _local_make_bundle(workspace: Path, mod_root: Path) -> ArtifactSource:
    """Worker-local bundle seam: package the bundle in memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_real_build_bundle(workspace, mod_root))


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    """Worker-local vmlinux seam: read ``vmlinux`` into memory as :class:`ArtifactBytes`."""
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))


_REMOTE_BUNDLE_NAME = "kdive-bundle.tar.gz"
_BUNDLE_TAR_TIMEOUT_S = 30 * 60


def transport_make_bundle(t: BuildTransport) -> _MakeBundle:
    """Return a ``_MakeBundle`` that tars the install bundle ON the build host (ADR-0342).

    The returned seam runs one ``tar`` over the transport that renames ``arch/x86/boot/bzImage``
    to ``boot/vmlinuz`` and stores the staged ``lib/modules`` tree, excluding the ``build`` and
    ``source`` back-reference symlinks (the same exclusion :func:`_real_build_bundle` applies
    in memory). The archive stays on the host; an :class:`ArtifactRemoteFile` referencing it is
    returned so :meth:`RemoteLibvirtBuild.publish` uploads it via a presigned PUT without the
    worker reading its bytes.

    Args:
        t: The build transport to run ``tar`` through.

    Returns:
        A callable ``(workspace, mod_root) -> ArtifactRemoteFile`` matching ``_MakeBundle``.
    """

    def _make(workspace: Path, mod_root: Path) -> ArtifactSource:
        bundle_path = str(workspace / _REMOTE_BUNDLE_NAME)
        argv = [
            "tar",
            "-czf",
            bundle_path,
            "--exclude=*/build",
            "--exclude=*/source",
            "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|",
            "-C",
            str(workspace),
            "arch/x86/boot/bzImage",
            "-C",
            str(mod_root),
            "lib/modules",
        ]
        result = t.run(argv, cwd=str(workspace), timeout_s=_BUNDLE_TAR_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "tar failed to package the kernel bundle on the build host",
                category=ErrorCategory.BUILD_FAILURE,
                details={"output": "module bundle", "stderr": result.stderr[-512:]},
            )
        return ArtifactRemoteFile(path=bundle_path, transport=t)

    return _make


def transport_vmlinux_source(t: BuildTransport) -> _ReadVmlinuxSource:
    """Return a ``_ReadVmlinuxSource`` yielding the host-resident ``vmlinux`` debuginfo.

    The returned seam never reads ``vmlinux``; it points an :class:`ArtifactRemoteFile` at
    ``<workspace>/vmlinux`` so :meth:`RemoteLibvirtBuild.publish` uploads it via a presigned
    PUT, hashing it on the host.

    Args:
        t: The build transport that can hash and upload the file.

    Returns:
        A callable ``(workspace: Path) -> ArtifactRemoteFile`` matching ``_ReadVmlinuxSource``.
    """

    def _source(workspace: Path) -> ArtifactSource:
        return ArtifactRemoteFile(path=str(workspace / "vmlinux"), transport=t)

    return _source


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


__all__ = [
    "ArtifactBytes",
    "ArtifactRemoteFile",
    "ArtifactSource",
    "RemoteLibvirtBuild",
    "transport_make_bundle",
    "transport_vmlinux_source",
]
