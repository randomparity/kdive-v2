"""Provider build-host mechanics shared by provider build planes."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - all calls use fixed argv and no shell
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import kdive.config as config
from kdive.build_configs.defaults import CatalogConfigFetch
from kdive.config.core_settings import BUILD_COMPONENT_ROOTS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_validation import (
    parse_gnu_build_id,
    patch_target_paths,
    snapshot_file_bytes,
)
from kdive.provider_components.catalog import load_fixture_catalog
from kdive.provider_components.local_paths import validate_local_component_path
from kdive.provider_components.references import (
    CatalogComponentRef,
    ComponentRef,
    LocalComponentRef,
)
from kdive.provider_components.requirements import ConfigRequirements
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

DEFAULT_BUILD_COMPONENT_ROOT = "/var/lib/kdive/build/components"
STDERR_TAIL = 2000
MAKE_TIMEOUT_S = 2 * 60 * 60
OBJCOPY_TIMEOUT_S = 60
GIT_APPLY_TIMEOUT_S = 120
RSYNC_TIMEOUT_S = 10 * 60

type Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]
type ReadConfig = Callable[[Path], str]
type RunStep = Callable[[Path], int]
type RunModulesInstall = Callable[[Path, Path], int]
type ReadBytes = Callable[[Path], bytes]
type ReadBuildId = Callable[[Path], str]


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


def make_checkout(kernel_src: str, secret_registry: SecretRegistry) -> Checkout:
    """Create the default checkout seam for a warm kernel source tree."""

    def _checkout(
        run_id: UUID, profile: ServerBuildProfile, workspace: Path, fragment_bytes: bytes
    ) -> None:
        real_checkout(
            kernel_src,
            profile,
            workspace,
            fragment_bytes,
            run_id=run_id,
            secret_registry=secret_registry,
        )

    return _checkout


def real_checkout(
    kernel_src: str,
    profile: ServerBuildProfile,
    workspace: Path,
    fragment_bytes: bytes,
    *,
    run_id: UUID,
    secret_registry: SecretRegistry,
) -> None:
    """Materialize a per-run workspace, merge config, and apply an optional patch."""
    sync_tree(kernel_src, workspace, secret_registry)
    merge_config(fragment_bytes, workspace, run_id)
    if profile.patch_ref is not None:
        apply_patch(profile.patch_ref, workspace, secret_registry)


def read_text_file(path: Path, *, category: ErrorCategory, file_label: str) -> str:
    """Read text or raise a categorized unreadable-file error."""
    try:
        return path.read_text()
    except OSError as exc:
        raise CategorizedError(
            f"{file_label} is missing or unreadable",
            category=category,
            details={"file": file_label},
        ) from exc


def read_bytes_file(path: Path, *, category: ErrorCategory, output: str) -> bytes:
    """Read bytes or raise a categorized unreadable-output error."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CategorizedError(
            f"{output} is missing or unreadable",
            category=category,
            details={"output": output},
        ) from exc


def launch_failure(tool: str, exc: OSError, *, category: ErrorCategory) -> CategorizedError:
    """Map a subprocess launch failure into the provider error taxonomy."""
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


def workspace_failure(op: str, path_label: str, exc: OSError) -> CategorizedError:
    """Map workspace filesystem failures into infrastructure failures."""
    return CategorizedError(
        f"build workspace {op} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": op, "path": path_label},
    )


def real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Read the final kernel ``.config``."""
    return read_text_file(
        workspace / ".config",
        category=ErrorCategory.CONFIGURATION_ERROR,
        file_label=".config",
    )


def real_read_kernel_image(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    """Read the built x86 bzImage."""
    return read_bytes_file(
        workspace / "arch/x86/boot/bzImage",
        category=ErrorCategory.BUILD_FAILURE,
        output="bzImage",
    )


def real_read_vmlinux(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    """Read the built ``vmlinux`` ELF."""
    return read_bytes_file(
        workspace / "vmlinux",
        category=ErrorCategory.BUILD_FAILURE,
        output="vmlinux",
    )


def real_run_make(workspace: Path) -> int:  # pragma: no cover - live_vm
    """Run the default parallel kernel build."""
    try:
        return subprocess.run(
            ["make", "-C", str(workspace), f"-j{os.cpu_count() or 1}"],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "make exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def real_run_olddefconfig(workspace: Path) -> int:  # pragma: no cover - live_vm
    """Run ``make olddefconfig``."""
    return run_make_target(workspace, ["olddefconfig"], "make olddefconfig")


def real_run_modules_install(workspace: Path, mod_root: Path) -> int:  # pragma: no cover
    """Run ``make modules_install INSTALL_MOD_PATH=<mod_root>``."""
    return run_make_target(
        workspace,
        [f"INSTALL_MOD_PATH={mod_root}", "modules_install"],
        "make modules_install",
    )


def run_make_target(workspace: Path, args: list[str], label: str) -> int:
    """Run ``make -C <workspace> <args...>`` and map launch/timeout faults."""
    try:
        return subprocess.run(
            ["make", "-C", str(workspace), *args],
            timeout=MAKE_TIMEOUT_S,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{label} exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details={"timeout_s": MAKE_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc


def real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux`` GNU build-id from its merged ``.notes`` section."""
    with tempfile.NamedTemporaryFile(suffix=".note") as note_file:
        try:
            subprocess.run(
                [
                    "objcopy",
                    "-O",
                    "binary",
                    "--only-section=.notes",
                    str(workspace / "vmlinux"),
                    note_file.name,
                ],
                timeout=OBJCOPY_TIMEOUT_S,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "objcopy exceeded the build-id extraction timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": OBJCOPY_TIMEOUT_S},
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CategorizedError(
                "objcopy failed to extract vmlinux notes",
                category=ErrorCategory.BUILD_FAILURE,
            ) from exc
        except OSError as exc:
            raise launch_failure(
                "objcopy", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
            ) from exc
        notes = read_bytes_file(
            Path(note_file.name),
            category=ErrorCategory.BUILD_FAILURE,
            output="vmlinux notes",
        )
    return parse_gnu_build_id(notes)


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


def build_failure(message: str, run_id: UUID) -> CategorizedError:
    """A build failure with run-id details."""
    return CategorizedError(
        message, category=ErrorCategory.BUILD_FAILURE, details={"run_id": str(run_id)}
    )


def merge_config(fragment_bytes: bytes, workspace: Path, run_id: UUID) -> None:  # pragma: no cover
    """Run base defconfig, merge the kdump fragment, and leave olddefconfig to the caller."""
    if run_make_target(workspace, ["defconfig"], "make defconfig") != 0:
        raise build_failure("make defconfig exited non-zero", run_id)
    fragment_path = workspace / "kdump.config.fragment"
    fragment_path.write_bytes(fragment_bytes)
    try:
        merge = subprocess.run(
            ["scripts/kconfig/merge_config.sh", "-m", ".config", str(fragment_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=MAKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise build_failure("merge_config.sh -m exceeded the build timeout", run_id) from exc
    except OSError as exc:
        raise launch_failure(
            "merge_config.sh", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
        ) from exc
    if merge.returncode != 0:
        raise build_failure("merge_config.sh -m exited non-zero", run_id)


def redacted_tail(text: str, secret_registry: SecretRegistry | None = None) -> str:
    """Redact known secrets and key/value pairs, then return the trailing stderr slice."""
    secret_registry = secret_registry or SecretRegistry()
    return Redactor(registry=secret_registry).redact_text(text)[-STDERR_TAIL:]


def apply_patch(
    patch_ref: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Apply the resolved patch ref to the workspace tree with no-op guards."""
    patch = resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    targets = patch_target_paths(patch.read_text(errors="replace"), strip=1)
    before = {rel: snapshot_file_bytes(workspace / rel) for rel in targets}
    try:
        result = subprocess.run(
            ["git", "apply", "-p1", "-v", "--", str(patch)],
            cwd=workspace,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
            timeout=GIT_APPLY_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "patch_ref does not apply within the timeout",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"timeout_s": GIT_APPLY_TIMEOUT_S},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if any(line.startswith("Skipped patch ") for line in result.stderr.splitlines()):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but skipped one or "
            "more files as already applied (the build workspace has no .git, so git fell "
            "back to context matching)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
    if targets and all(snapshot_file_bytes(workspace / rel) == before[rel] for rel in targets):
        raise CategorizedError(
            "patch_ref was silently skipped: git apply reported success but left the kernel "
            "tree unchanged (the build workspace has no .git, so git fell back to context "
            "matching and treated the patch as already applied)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"targets": sorted(str(rel) for rel in targets)},
        )


def sync_tree(
    kernel_src: str, workspace: Path, secret_registry: SecretRegistry | None = None
) -> None:
    """Mirror the warm kernel source tree into ``workspace`` with ``rsync -a --delete``."""
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
        raise workspace_failure("mkdir", "build_workspace", exc) from exc
    try:
        result = subprocess.run(
            ["rsync", "-a", "--delete", "--", f"{source}/", f"{workspace}/"],
            capture_output=True,
            text=True,
            timeout=RSYNC_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "rsync exceeded the workspace sync timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": RSYNC_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise launch_failure("rsync", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": redacted_tail(result.stderr, secret_registry)},
        )
