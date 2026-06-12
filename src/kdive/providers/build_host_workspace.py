"""Build-host workspace checkout, config merge, patch, and sync helpers."""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - all calls use fixed argv and no shell
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_validation import patch_target_paths, snapshot_file_bytes
from kdive.providers.build_host_config import resolve_local_ref
from kdive.providers.build_host_execution import (
    MAKE_TIMEOUT_S,
    build_failure,
    launch_failure,
    run_make_target,
    workspace_failure,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

STDERR_TAIL = 2000
GIT_APPLY_TIMEOUT_S = 120
RSYNC_TIMEOUT_S = 10 * 60

type Checkout = Callable[[UUID, ServerBuildProfile, Path, bytes], None]


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
