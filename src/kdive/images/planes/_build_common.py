"""Shared mechanics for rootfs image build planes."""

from __future__ import annotations

import hashlib
import re
import subprocess  # noqa: S404 - libguestfs tools invoked with fixed argv, no shell
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

_DIGEST_CHUNK = 1024 * 1024
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def validate_image_name(name: str) -> None:
    """Reject image names that could escape the build workspace."""
    if _NAME_RE.fullmatch(name):
        return
    raise CategorizedError(
        "image name must match ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ (it becomes a filename)",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"name": name},
    )


def run_guestfs_tool(
    argv: list[str],
    *,
    stage: str,
    timeout_s: int,
    missing_message: str,
    failure_message: str | None = None,
    input_text: str | None = None,
) -> None:
    """Run a fixed-argv libguestfs tool, mapping failures onto categorized errors."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted inputs
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CategorizedError(
            missing_message,
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"stage": stage, "tool": argv[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{stage} exceeded its timeout",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stage": stage, "tool": argv[0], "timeout_s": timeout_s},
        ) from exc
    except OSError as exc:
        raise CategorizedError(
            f"failed to launch {argv[0]} for {stage}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stage": stage, "tool": argv[0], "error": type(exc).__name__},
        ) from exc
    if result.returncode != 0:
        raise CategorizedError(
            failure_message or f"{stage} failed",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stage": stage, "tool": argv[0], "stderr": result.stderr[-2000:]},
        )


@contextmanager
def build_workspace(workspace: Path, *, prefix: str) -> Iterator[Path]:
    """Create the persistent workspace and yield a temporary per-build directory."""
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace, prefix=prefix) as work:
        yield Path(work)


def publish_qcow2(workspace: Path, *, image_name: str, scratch: Path) -> Path:
    """Atomically publish a scratch qcow2 into the persistent workspace."""
    qcow2 = workspace / f"{image_name}.qcow2"
    scratch.replace(qcow2)
    return qcow2


def digest_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content digest of ``path``."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DIGEST_CHUNK), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
