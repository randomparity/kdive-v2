"""Runtime version reporting: package version + commit SHA + release/dev flag (ADR-0041).

`full_version()` is the display string used by `--version` and the startup log line:
`X.Y.Z+g<sha>` for a release build, `X.Y.Z-dev+g<sha>` otherwise. Commit and release
status resolve, first hit wins, from: a baked `kdive._buildinfo` module (present only in a
built artifact), live git (a dev checkout), or unknown. No git subprocess runs at import
time — resolution is lazy and memoized for the process lifetime, with `cache_clear()`
exposed for tests.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

_GIT_TIMEOUT = 3.0


def package_version() -> str:
    """Return the installed distribution version (`[project].version`), or ``0.0.0``."""
    try:
        return _dist_version("kdive")
    except PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Resolved version facts for the running process."""

    version: str
    commit: str | None
    is_release: bool


def _git(*args: str) -> str | None:
    """Run a read-only ``git`` command; return stripped stdout, or ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _from_baked() -> VersionInfo | None:
    """Read `(COMMIT, RELEASE)` from the baked `_buildinfo` module, if present and valid."""
    try:
        from kdive import _buildinfo  # ty: ignore[unresolved-import]  # only present in built artifacts
    except ImportError:
        return None
    commit = getattr(_buildinfo, "COMMIT", None)
    release = getattr(_buildinfo, "RELEASE", None)
    if not isinstance(commit, str) or not isinstance(release, bool):
        return None
    return VersionInfo(package_version(), commit, release)


def _from_git() -> VersionInfo | None:
    """Resolve from live git, or ``None`` when not in a usable checkout."""
    commit = _git("rev-parse", "--short", "HEAD")
    if commit is None:
        return None
    version = package_version()
    exact = _git("describe", "--tags", "--exact-match", "HEAD")
    clean = _git("status", "--porcelain") == ""
    return VersionInfo(version, commit, exact == f"v{version}" and clean)


@lru_cache(maxsize=1)
def version_info() -> VersionInfo:
    """Resolve `(version, commit, is_release)` once per process: baked → git → unknown."""
    return _from_baked() or _from_git() or VersionInfo(package_version(), None, False)


def full_version() -> str:
    """Return the display string, e.g. ``0.2.0+g1a2b3c4`` or ``0.2.0-dev+g1a2b3c4``."""
    info = version_info()
    suffix = "" if info.is_release else "-dev"
    commit = f"+g{info.commit}" if info.commit else ""
    return f"{info.version}{suffix}{commit}"
