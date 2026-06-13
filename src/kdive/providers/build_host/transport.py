"""BuildTransport port + LocalBuildTransport for the build-host seam (ADR-0342).

:class:`BuildTransport` is the structural protocol any build-host implementation satisfies.
:class:`LocalBuildTransport` wraps local subprocess and filesystem primitives and is the
behavior-preserving replacement for the inline calls in ``execution.py``. Later tasks
add ``SshBuildTransport`` behind the same port.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess  # noqa: S404 - fixed argv only, no shell
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.execution import launch_failure

# (url, data, headers) -> etag string (may include surrounding quotes)
type HttpPut = Callable[[str, bytes, dict[str, str]], str]


@dataclass(slots=True, frozen=True)
class CommandResult:
    """The captured result of a remote or local subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str


class BuildTransport(Protocol):
    """Structural port for build-host primitives.

    All methods must be implementable both locally (subprocess + filesystem) and
    remotely (SSH + presigned object-store channels) without altering callers.
    """

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* with a hard *timeout_s* deadline."""
        ...

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Read *path* as raw bytes."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path*, creating or overwriting the file."""
        ...

    def clone(self, remote: str, ref: str, dest: str) -> None:
        """Clone *remote* at *ref* into *dest* (SSH implementation only)."""
        ...

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* via *presigned* and return the ETag (quotes stripped)."""
        ...

    def cleanup(self, path: str) -> None:
        """Remove *path* — directory tree or single file — best-effort."""
        ...


def _default_http_put(url: str, data: bytes, headers: dict[str, str]) -> str:
    """Perform an HTTP PUT and return the response ETag header value."""
    # noqa: S310 - url comes from a worker-minted PresignedUpload, not user input
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return resp.headers["ETag"]


class LocalBuildTransport:
    """Implements :class:`BuildTransport` with local subprocess and filesystem primitives.

    Args:
        http_put: Injectable HTTP PUT callable for :meth:`upload_file`. Defaults to a
            urllib-based implementation. Inject a fake in tests to avoid network calls.
    """

    def __init__(self, *, http_put: HttpPut | None = None) -> None:
        self._http_put: HttpPut = http_put if http_put is not None else _default_http_put

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* locally via subprocess and return a :class:`CommandResult`.

        Maps :class:`FileNotFoundError` to ``MISSING_DEPENDENCY`` and
        :class:`subprocess.TimeoutExpired` to ``BUILD_FAILURE`` via the shared helpers in
        ``execution.py``.

        Note: this captures stdout/stderr (unlike the bare ``real_run_make`` which did not).
        The return code still drives the orchestrator; captured output is available for
        the SSH transport and for redaction. No existing code reads make's stdout from the
        worker's fd — the build status flows through the return code only.
        """
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout_s,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "command exceeded the build timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": timeout_s},
            ) from exc
        except OSError as exc:
            raise launch_failure(
                argv[0], exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
            ) from exc
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text."""
        return Path(path).read_text(encoding="utf-8")

    def read_bytes(self, path: str) -> bytes:
        """Read *path* as bytes using :meth:`Path.read_bytes`."""
        return Path(path).read_bytes()

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* using :meth:`Path.write_bytes`."""
        Path(path).write_bytes(data)

    def clone(self, remote: str, ref: str, dest: str) -> None:  # noqa: ARG002
        """Reject git clone — local builds use the warm tree, not a fresh clone.

        Raises:
            CategorizedError: Always raises with ``CONFIGURATION_ERROR``.
        """
        raise CategorizedError(
            "git provenance is not valid for a local build host",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* to the presigned URL and return the ETag (quotes stripped).

        Args:
            path: Local filesystem path to the file to upload.
            presigned: URL and required headers for the presigned PUT.

        Returns:
            The ETag returned by the object store, with surrounding quotes removed.
        """
        data = Path(path).read_bytes()
        raw_etag = self._http_put(presigned.url, data, presigned.required_headers)
        return raw_etag.strip('"')

    def cleanup(self, path: str) -> None:
        """Remove *path* — directory tree via :func:`shutil.rmtree`, file via :func:`Path.unlink`.

        Errors are suppressed (best-effort cleanup matching the workspace staging pattern).
        """
        target = Path(path)
        if target.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                target.unlink()
