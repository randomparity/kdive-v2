"""Provider-local component path validation (ADR-0065)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory


def validate_local_component_path(
    path: str,
    *,
    allowed_roots: Iterable[Path],
    sha256: str | None = None,
) -> Path:
    """Return a resolved regular file path after provider-root and digest validation."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise _config_error("local component path must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _config_error("local component path does not exist") from exc

    roots = [root.resolve(strict=False) for root in allowed_roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise _config_error("local component path is outside provider allowed roots")
    if not resolved.is_file():
        raise _config_error("local component path is not a regular file")
    if not os.access(resolved, os.R_OK):
        raise _config_error("local component path is not readable")
    if sha256 is not None and _file_sha256(resolved) != sha256.removeprefix("sha256:"):
        raise _config_error("local component sha256 does not match")
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_error(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR)
