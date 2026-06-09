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

import os
import shutil
import subprocess  # noqa: S404 - make is invoked with a fixed argv, no shell
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from kdive.components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.components.catalog import load_fixture_catalog
from kdive.components.local_paths import validate_local_component_path
from kdive.components.references import ComponentRef, LocalComponentRef
from kdive.components.requirements import ConfigRequirements, validate_config_requirements
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.providers.build_validation import (
    parse_gnu_build_id,
)
from kdive.providers.ports import BuildOutput
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

_WORKSPACE_ENV = "KDIVE_BUILD_WORKSPACE"
_KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"
_BUILD_COMPONENT_ROOTS_ENV = "KDIVE_BUILD_COMPONENT_ROOTS"
_DEFAULT_WORKSPACE = "/var/lib/kdive/build"
_DEFAULT_BUILD_COMPONENT_ROOT = "/var/lib/kdive/build/components"
_RETENTION_CLASS = "build"
# Trailing chars of a redacted rsync/git-apply stderr placed in error details (bounded so a
# large/noisy failure log cannot bloat a persisted error record).
_STDERR_TAIL = 2000
_MAKE_TIMEOUT_S = 2 * 60 * 60
_OBJCOPY_TIMEOUT_S = 60
_GIT_APPLY_TIMEOUT_S = 120
_RSYNC_TIMEOUT_S = 10 * 60

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
    enabled = {
        line.split("=", 1)[0]
        for line in config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith("=y")
    }
    return [group for group in _REQUIRED_CONFIG if not any(opt in enabled for opt in group)]


type _Checkout = Callable[[UUID, ServerBuildProfile, Path], None]
type _ReadConfig = Callable[[Path], str]
type _RunOlddefconfig = Callable[[Path], int]
type _RunMake = Callable[[Path], int]
type _ReadBytes = Callable[[Path], bytes]
type _ReadBuildId = Callable[[Path], str]


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
        read_config: _ReadConfig,
        run_make: _RunMake,
        read_kernel_image: _ReadBytes,
        read_vmlinux: _ReadBytes,
        read_build_id: _ReadBuildId,
        secret_registry: SecretRegistry,
        allowed_component_roots: list[Path] | None = None,
    ) -> None:
        self._tenant = tenant
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
        workspace_root = Path(os.environ.get(_WORKSPACE_ENV, _DEFAULT_WORKSPACE))
        kernel_src = os.environ.get(_KERNEL_SRC_ENV, "")
        allowed_component_roots = _build_component_roots_from_env()
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_make_checkout(kernel_src, allowed_component_roots, secret_registry),
            run_olddefconfig=_real_run_olddefconfig,
            read_config=_real_read_config,
            run_make=_real_run_make,
            read_kernel_image=_real_read_kernel_image,
            read_vmlinux=_real_read_vmlinux,
            read_build_id=_real_read_build_id,
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
        self._checkout(run_id, profile, workspace)
        if self._run_olddefconfig(workspace) != 0:
            raise CategorizedError(
                "make olddefconfig exited non-zero",
                category=ErrorCategory.BUILD_FAILURE,
                details={"run_id": str(run_id)},
            )
        config_text = self._read_config(workspace)
        missing = _missing_config_groups(config_text)
        if missing:
            raise CategorizedError(
                "kernel .config omits a required kdump/debuginfo option",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"missing_any_of": [list(group) for group in missing]},
            )
        if profile.profile_requirements is not None:
            requirements = _load_profile_config_requirements(
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
        """Validate that a build config ref is available within provider roots."""
        _resolve_config_ref(ref, allowed_component_roots=self._allowed_component_roots)

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


def _load_profile_config_requirements(provider: str, name: str) -> ConfigRequirements:
    profile = load_fixture_catalog().profile(provider, name)
    if profile is None:
        raise CategorizedError(
            "unknown build profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name},
        )
    return profile.requires.config


def _build_component_roots_from_env() -> list[Path]:
    raw = os.environ.get(_BUILD_COMPONENT_ROOTS_ENV)
    if raw is None:
        return [Path(_DEFAULT_BUILD_COMPONENT_ROOT)]
    return [Path(part) for part in raw.split(":") if part]


def _make_checkout(
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


def _real_checkout(
    kernel_src: str,
    profile: ServerBuildProfile,
    workspace: Path,
    *,
    secret_registry: SecretRegistry,
    allowed_component_roots: list[Path] | None = None,
) -> None:
    """Materialize a warm per-Run workspace, stage the ``.config``, apply any patch.

    Steps run in order so the resetting rsync (sync) precedes config-staging and patch
    application; see ADR-0053 for the per-step failure contract. The rsync sync and the
    later ``make`` run only on a real build host (``live_vm``); this composition itself is
    unit-tested with the steps stubbed.
    """
    _sync_tree(kernel_src, workspace, secret_registry)
    _stage_config(
        profile.config,
        workspace,
        allowed_component_roots=allowed_component_roots or [Path(_DEFAULT_BUILD_COMPONENT_ROOT)],
    )
    if profile.patch_ref is not None:
        _apply_patch(profile.patch_ref, workspace, secret_registry)


def _read_text_file(path: Path, *, category: ErrorCategory, file_label: str) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise CategorizedError(
            f"{file_label} is missing or unreadable",
            category=category,
            details={"file": file_label},
        ) from exc


def _read_bytes_file(path: Path, *, category: ErrorCategory, output: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CategorizedError(
            f"{output} is missing or unreadable",
            category=category,
            details={"output": output},
        ) from exc


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


def _workspace_failure(op: str, path_label: str, exc: OSError) -> CategorizedError:
    return CategorizedError(
        f"build workspace {op} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": op, "path": path_label},
    )


def _real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    return _read_text_file(
        workspace / ".config",
        category=ErrorCategory.CONFIGURATION_ERROR,
        file_label=".config",
    )


def _real_read_kernel_image(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return _read_bytes_file(
        workspace / "arch/x86/boot/bzImage",
        category=ErrorCategory.BUILD_FAILURE,
        output="bzImage",
    )


def _real_read_vmlinux(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return _read_bytes_file(
        workspace / "vmlinux",
        category=ErrorCategory.BUILD_FAILURE,
        output="vmlinux",
    )


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


def _real_run_olddefconfig(workspace: Path) -> int:  # pragma: no cover - live_vm
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted workspace
            ["make", "-C", str(workspace), "olddefconfig"],
            timeout=_MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make olddefconfig exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": _MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise _launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def _real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux``'s GNU build-id via the tested note parser.

    Dumps the ``.notes`` section as raw bytes with ``objcopy`` and feeds them to
    :func:`parse_gnu_build_id`, so the shipped extraction is the unit-tested logic (not a
    locale-fragile ``readelf`` text scrape). The kernel's ``vmlinux.lds`` merges every ELF
    note into one ``.notes`` section, so the build-id is not in a standalone
    ``.note.gnu.build-id`` section the way a userspace binary's is; the parser scans the
    stream for the ``NT_GNU_BUILD_ID`` note regardless of the other notes present.
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
        notes = _read_bytes_file(
            Path(note_file.name),
            category=ErrorCategory.BUILD_FAILURE,
            output="vmlinux notes",
        )
    return parse_gnu_build_id(notes)


def _ref_error(kind: str, message: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` for a bad ref; ``details`` names the field, never its value."""
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )


def _resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref (``config_ref``/``patch_ref``) to an existing local file.

    Accepts a ``file:///abs/path`` URL (empty authority) or a bare absolute path; rejects a
    non-local scheme, a ``file://`` URL with a host, a non-absolute path, or a path that is
    not an existing regular file. The submitted ref value is never echoed in the error.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (``details={"kind": kind}``) for any
            unsupported or unresolvable reference.
    """
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise _ref_error(kind, "config/patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise _ref_error(kind, "config/patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise _ref_error(kind, "config/patch ref must be an absolute path")
    if not path.is_file():
        raise _ref_error(kind, "config/patch ref does not resolve to a readable file")
    return path


def _resolve_config_ref(ref: ComponentRef, *, allowed_component_roots: list[Path]) -> Path:
    if not isinstance(ref, LocalComponentRef):
        raise _ref_error("config", "config component ref must be local for local-libvirt builds")
    return validate_local_component_path(
        ref.path,
        allowed_roots=allowed_component_roots,
        sha256=ref.sha256,
    )


def _stage_config(
    config: ComponentRef,
    workspace: Path,
    *,
    allowed_component_roots: list[Path] | None = None,
) -> None:
    """Copy the resolved config component to ``workspace/.config``."""
    source = _resolve_config_ref(
        config,
        allowed_component_roots=allowed_component_roots or [Path(_DEFAULT_BUILD_COMPONENT_ROOT)],
    )
    try:
        shutil.copyfile(source, workspace / ".config")
    except OSError as exc:
        raise _workspace_failure("copy_config", ".config", exc) from exc


def _redacted_tail(text: str, secret_registry: SecretRegistry | None = None) -> str:
    """Redact known secrets/``key=value`` pairs, then return the trailing ``_STDERR_TAIL`` chars."""
    secret_registry = secret_registry or SecretRegistry()
    return Redactor(registry=secret_registry).redact_text(text)[-_STDERR_TAIL:]


def _apply_patch(
    patch_ref: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Apply the resolved ``patch_ref`` to the workspace tree with ``git apply -p1``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the ref is unresolvable (via
            :func:`_resolve_local_ref`) or the patch does not apply (a redacted stderr tail
            is placed in ``details``); ``MISSING_DEPENDENCY`` if ``git`` is absent.
    """
    patch = _resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell; -- ends option parsing
            ["git", "apply", "-p1", "--", str(patch)],
            cwd=workspace,
            capture_output=True,
            text=True,
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


def _sync_tree(
    kernel_src: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Mirror the warm ``kernel_src`` tree into ``workspace`` with ``rsync -a --delete``.

    Creates ``workspace`` (and missing parents) first, since ``build()`` does not and rsync
    does not create missing parent directories.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``kernel_src`` is empty, not an
            absolute path, the filesystem root, or not a directory; ``MISSING_DEPENDENCY``
            if ``rsync`` is absent; ``INFRASTRUCTURE_FAILURE`` on a non-zero rsync exit
            (redacted stderr in details).
    """
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
        raise _workspace_failure("mkdir", "build_workspace", exc) from exc
    # `--` ends option parsing so a path is never mistaken for an rsync flag; the trailing
    # slash on the source copies its *contents* into the workspace, not a nested dir.
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
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


__all__ = [
    "LocalLibvirtBuild",
]
