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
``providers.build_validation`` helpers and duplicates the build mechanics. The slow,
environment-bound operations are **injected seams** that default to the real implementations,
so unit tests cover the orchestration/error contract without a toolchain; the real ``make``
path is exercised under the ``live_vm`` gate. `build()` is synchronous; the async build
handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess  # noqa: S404 - make is invoked with a fixed argv, no shell
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.provider_components.local_paths import validate_local_component_path
from kdive.provider_components.references import ComponentRef, LocalComponentRef
from kdive.providers.build_validation import (
    parse_gnu_build_id,
    patch_target_paths,
    snapshot_file_bytes,
)
from kdive.providers.ports import BuildOutput
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "build"
_WORKSPACE_ENV = "KDIVE_BUILD_WORKSPACE"
_KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"
_BUILD_COMPONENT_ROOTS_ENV = "KDIVE_BUILD_COMPONENT_ROOTS"
_DEFAULT_WORKSPACE = "/var/lib/kdive/build"
_DEFAULT_BUILD_COMPONENT_ROOT = "/var/lib/kdive/build/components"
# Trailing chars of a redacted rsync/git-apply stderr placed in error details (bounded so a
# large/noisy failure log cannot bloat a persisted error record).
_STDERR_TAIL = 2000
_MAKE_TIMEOUT_S = 2 * 60 * 60
_OBJCOPY_TIMEOUT_S = 60
_GIT_APPLY_TIMEOUT_S = 120
_RSYNC_TIMEOUT_S = 10 * 60
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


type _Checkout = Callable[[UUID, ServerBuildProfile, Path], None]
type _ReadConfig = Callable[[Path], str]
type _RunStep = Callable[[Path], int]
type _RunModulesInstall = Callable[[Path, Path], int]
type _BuildBundle = Callable[[Path, Path], bytes]
type _ReadBytes = Callable[[Path], bytes]
type _ReadBuildId = Callable[[Path], str]
type _StagingFactory = Callable[[], Path]


def _missing_config_groups(config_text: str) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text`` (``CONFIG_X=y``)."""
    enabled = {
        line.split("=", 1)[0]
        for line in config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith("=y")
    }
    return [group for group in _REQUIRED_CONFIG if not any(opt in enabled for opt in group)]


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
        allowed_component_roots: list[Path] | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._allowed_component_roots = allowed_component_roots or [
            Path(_DEFAULT_BUILD_COMPONENT_ROOT)
        ]
        self._store_factory = store_factory
        self._store: _StorePort | None = None
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
        workspace_root = Path(os.environ.get(_WORKSPACE_ENV, _DEFAULT_WORKSPACE))
        kernel_src = os.environ.get(_KERNEL_SRC_ENV, "")
        allowed_component_roots = _build_component_roots_from_env()
        return cls(
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_make_checkout(kernel_src, allowed_component_roots, secret_registry),
            run_olddefconfig=_real_run_olddefconfig,
            read_config=_real_read_config,
            run_make=_real_run_make,
            run_modules_install=_real_run_modules_install,
            build_bundle=_real_build_bundle,
            read_vmlinux=_real_read_vmlinux,
            read_build_id=_real_read_build_id,
            staging_factory=_real_staging_factory,
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
        self._checkout(run_id, profile, workspace)
        if self._run_olddefconfig(workspace) != 0:
            raise _build_failure("make olddefconfig exited non-zero", run_id)
        missing = _missing_config_groups(self._read_config(workspace))
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
        """Validate that a build config ref is available within provider roots."""
        _resolve_config_ref(ref, allowed_component_roots=self._allowed_component_roots)

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


def _build_failure(message: str, run_id: UUID) -> CategorizedError:
    return CategorizedError(
        message, category=ErrorCategory.BUILD_FAILURE, details={"run_id": str(run_id)}
    )


def _build_component_roots_from_env() -> list[Path]:
    raw = os.environ.get(_BUILD_COMPONENT_ROOTS_ENV)
    if raw is None:
        return [Path(_DEFAULT_BUILD_COMPONENT_ROOT)]
    return [Path(part) for part in raw.split(":") if part]


def _ref_error(kind: str, message: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` for a bad ref; ``details`` names the field, never its value."""
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )


def _resolve_config_ref(ref: ComponentRef, *, allowed_component_roots: list[Path]) -> Path:
    if not isinstance(ref, LocalComponentRef):
        raise _ref_error("config", "config component ref must be local for remote-libvirt builds")
    return validate_local_component_path(
        ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
    )


def _resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref (``patch_ref``) to an existing local file.

    Accepts a ``file:///abs/path`` URL (empty authority) or a bare absolute path; rejects a
    non-local scheme, a ``file://`` URL with a host, a non-absolute path, or a path that is not
    an existing regular file. The submitted ref value is never echoed in the error.
    """
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise _ref_error(kind, "patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise _ref_error(kind, "patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise _ref_error(kind, "patch ref must be an absolute path")
    if not path.is_file():
        raise _ref_error(kind, "patch ref does not resolve to a readable file")
    return path


def _redacted_tail(text: str, secret_registry: SecretRegistry | None = None) -> str:
    """Redact known secrets/``key=value`` pairs, then return the trailing ``_STDERR_TAIL`` chars."""
    secret_registry = secret_registry or SecretRegistry()
    return Redactor(registry=secret_registry).redact_text(text)[-_STDERR_TAIL:]


def _launch_failure(tool: str, exc: OSError, *, category: ErrorCategory) -> CategorizedError:
    if isinstance(exc, FileNotFoundError):
        return CategorizedError(
            f"{tool} is required for kernel builds",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": tool},
        )
    return CategorizedError(
        f"{tool} failed to launch",
        category=category,
        details={"tool": tool, "op": "launch"},
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


def _make_checkout(  # pragma: no cover - live_vm
    kernel_src: str, allowed_component_roots: list[Path], secret_registry: SecretRegistry
) -> _Checkout:
    def _checkout(run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        _real_checkout(
            kernel_src,
            profile,
            workspace,
            allowed_component_roots=allowed_component_roots,
            secret_registry=secret_registry,
        )

    return _checkout


def _real_checkout(  # pragma: no cover - live_vm
    kernel_src: str,
    profile: ServerBuildProfile,
    workspace: Path,
    *,
    secret_registry: SecretRegistry,
    allowed_component_roots: list[Path] | None = None,
) -> None:
    """Materialize a warm per-Run workspace, stage the ``.config``, apply any patch.

    Steps run in order so the resetting rsync precedes config-staging and patch application;
    the rsync and later ``make`` run only on a real build host (``live_vm``).
    """
    roots = allowed_component_roots or [Path(_DEFAULT_BUILD_COMPONENT_ROOT)]
    _sync_tree(kernel_src, workspace, secret_registry)
    _stage_config(profile.config, workspace, allowed_component_roots=roots)
    if profile.patch_ref is not None:
        _apply_patch(profile.patch_ref, workspace, secret_registry)


def _sync_tree(  # pragma: no cover - live_vm
    kernel_src: str, workspace: Path, secret_registry: SecretRegistry
) -> None:
    """Mirror the warm ``kernel_src`` tree into ``workspace`` with ``rsync -a --delete``."""
    source = Path(kernel_src) if kernel_src else None
    if source is None or not source.is_absolute() or source == source.parent or not source.is_dir():
        raise CategorizedError(
            "KDIVE_KERNEL_SRC must be an absolute path to an existing kernel source tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("rsync") is None:
        raise CategorizedError(
            "rsync is required to materialize the warm kernel tree",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CategorizedError(
            "build workspace mkdir failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "mkdir", "path": "build_workspace"},
        ) from exc
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell; -- ends option parsing
            ["rsync", "-a", "--delete", "--", f"{source}/", f"{workspace}/"],
            capture_output=True,
            text=True,
            timeout=_RSYNC_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "rsync exceeded the workspace sync timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _RSYNC_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise _launch_failure("rsync", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": _redacted_tail(result.stderr, secret_registry)},
        )


def _stage_config(  # pragma: no cover - live_vm
    config: ComponentRef, workspace: Path, *, allowed_component_roots: list[Path]
) -> None:
    """Copy the resolved config component to ``workspace/.config``."""
    source = _resolve_config_ref(config, allowed_component_roots=allowed_component_roots)
    try:
        shutil.copyfile(source, workspace / ".config")
    except OSError as exc:
        raise CategorizedError(
            "build workspace copy_config failed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "copy_config", "path": ".config"},
        ) from exc


def _apply_patch(patch_ref: str, workspace: Path, secret_registry: SecretRegistry) -> None:
    """Apply the resolved ``patch_ref`` to the workspace tree with ``git apply -p1``.

    The workspace is a ``.git``-less rsync of the kernel tree, so ``git apply`` falls back
    to context matching and can exit 0 while silently skipping the patch (issue #227) —
    which would ship an unpatched kernel reported as a successful build. Two complementary
    guards reject that: ``git apply -v`` (under ``LC_ALL=C``) names skipped files on stderr
    as ``Skipped patch '<file>'.``, and as a locale-independent backstop the files the patch
    targets are snapshotted before/after and a no-op apply is failed.
    """
    patch = _resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    targets = patch_target_paths(patch.read_text(errors="replace"), strip=1)
    before = {rel: snapshot_file_bytes(workspace / rel) for rel in targets}
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell; -- ends option parsing
            ["git", "apply", "-p1", "-v", "--", str(patch)],
            cwd=workspace,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=_GIT_APPLY_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "patch_ref does not apply within the timeout",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"timeout_s": _GIT_APPLY_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": _redacted_tail(result.stderr, secret_registry)},
        )
    if any(line.startswith("Skipped patch ") for line in result.stderr.splitlines()):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but skipped one or "
            "more files as already applied (the build workspace has no .git, so git fell "
            "back to context matching)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": _redacted_tail(result.stderr, secret_registry)},
        )
    if targets and all(snapshot_file_bytes(workspace / rel) == before[rel] for rel in targets):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but left the kernel "
            "tree unchanged (the build workspace has no .git, so git fell back to context "
            "matching and treated the patch as already applied)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"targets": sorted(str(rel) for rel in targets)},
        )


def _real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    try:
        return (workspace / ".config").read_text()
    except OSError as exc:
        raise CategorizedError(
            ".config is missing or unreadable",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"file": ".config"},
        ) from exc


def _real_read_vmlinux(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    try:
        return (workspace / "vmlinux").read_bytes()
    except OSError as exc:
        raise CategorizedError(
            "vmlinux is missing or unreadable",
            category=ErrorCategory.BUILD_FAILURE,
            details={"output": "vmlinux"},
        ) from exc


def _real_run_olddefconfig(workspace: Path) -> int:  # pragma: no cover - live_vm
    return _run_make_target(workspace, ["olddefconfig"], "make olddefconfig")


def _real_run_make(workspace: Path) -> int:  # pragma: no cover - live_vm
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted workspace
            ["make", "-C", str(workspace), f"-j{os.cpu_count() or 1}"],
            timeout=_MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": _MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise _launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def _real_run_modules_install(workspace: Path, mod_root: Path) -> int:  # pragma: no cover - live_vm
    """``make modules_install INSTALL_MOD_PATH=<mod_root>`` into the private staging root."""
    return _run_make_target(
        workspace,
        [f"INSTALL_MOD_PATH={mod_root}", "modules_install"],
        "make modules_install",
    )


def _run_make_target(workspace: Path, args: list[str], label: str) -> int:
    """Run ``make -C <workspace> <args…>``; map timeout/launch faults to typed errors."""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted workspace
            ["make", "-C", str(workspace), *args],
            timeout=_MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{label} exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": _MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise _launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def _real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux``'s GNU build-id via the tested note parser.

    Dumps the ``.notes`` section as raw bytes with ``objcopy`` and feeds them to
    :func:`parse_gnu_build_id`; the kernel's ``vmlinux.lds`` merges every ELF note into one
    ``.notes`` section, so the build-id is not in a standalone ``.note.gnu.build-id`` section.
    """
    with tempfile.NamedTemporaryFile(suffix=".note") as note_file:
        try:
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    "objcopy",
                    "-O",
                    "binary",
                    "--only-section=.notes",
                    str(workspace / "vmlinux"),
                    note_file.name,
                ],
                timeout=_OBJCOPY_TIMEOUT_S,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "objcopy exceeded the build-id extraction timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": _OBJCOPY_TIMEOUT_S},
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CategorizedError(
                "objcopy failed to extract vmlinux notes",
                category=ErrorCategory.BUILD_FAILURE,
            ) from exc
        except OSError as exc:
            raise _launch_failure(
                "objcopy", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
            ) from exc
        notes = Path(note_file.name).read_bytes()
    return parse_gnu_build_id(notes)


__all__ = ["RemoteLibvirtBuild"]
