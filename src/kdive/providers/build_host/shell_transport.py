"""ShellBuildTransport: the shared BuildTransport surface over a single host-exec primitive.

The two remote build transports — ``SshBuildTransport`` (over ``ssh``) and
``GuestExecBuildTransport`` (over the qemu-guest-agent exec channel) — share their entire
:class:`BuildTransport` surface and differ only in the primitive that runs one ``argv`` on
the host. This base implements ``run``/``read_text``/``read_bytes``/``clone``/``upload_file``/
``cleanup`` in terms of an abstract ``_run_remote``; subclasses provide ``_run_remote`` and
``write_bytes`` (whose framing — a stdin stream vs an in-line base64 pipeline — the
single-argv primitive does not generalize).
"""

from __future__ import annotations

import base64
import logging

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.workspace import redacted_tail
from kdive.providers.ports.build_transport import CommandResult
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# Characters git/ssh/curl would mis-parse as options or that could split an argv.
_UNSAFE_CHARS = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
)

# read_bytes/read_text base64-capture a whole remote file into memory. These reads are small
# (.config, the build-id note) — cap the captured (base64) output well above any legitimate
# value so a mis-pointed path cannot exhaust worker memory.
_MAX_REMOTE_READ_B64_BYTES = 8 * 1024 * 1024

# Clone operations get a longer budget than the small reads.
_CLONE_TIMEOUT_S = 10 * 60
_UPLOAD_TIMEOUT_S = 5 * 60


def _validate_git_arg(value: str, field: str) -> None:
    """Reject a git remote or ref that could be parsed as an option or inject a command.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the value starts with ``-`` or
            contains a control character or newline.
    """
    if value.startswith("-"):
        raise CategorizedError(
            f"{field} must not start with '-' (would be parsed as a git option)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )
    if any(c in _UNSAFE_CHARS for c in value):
        raise CategorizedError(
            f"{field} contains a control character or newline",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )


def _validate_url(url: str) -> None:
    """Reject a URL containing a control character before it reaches a remote command."""
    if any(c in _UNSAFE_CHARS for c in url):
        raise CategorizedError(
            "presigned URL contains a control character",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _extract_etag_from_headers(header_dump: str) -> str:
    """Parse the ETag value from a curl ``-D -`` header dump.

    Returns an empty string if the ETag header is absent (caller strips quotes and stores
    as-is; the worker HEADs the object afterward for verification).
    """
    for line in header_dump.splitlines():
        if line.lower().startswith("etag:"):
            return line.split(":", 1)[1].strip()
    return ""


class ShellBuildTransport:
    """Common BuildTransport methods over an abstract single-argv host-exec primitive.

    Subclasses MUST set ``self._secret_registry`` (for redacting secrets out of error details)
    and implement :meth:`_run_remote` and :meth:`write_bytes`.
    """

    _secret_registry: SecretRegistry

    # ------------------------------------------------------------------
    # Subclass-provided primitives
    # ------------------------------------------------------------------

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* on the host with a hard *timeout_s* deadline."""
        raise NotImplementedError

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* on the host (framing is subclass-specific)."""
        raise NotImplementedError

    def _upload_url_detail(self, url: str) -> str:
        """The form of a presigned URL safe to place in an error detail (raw by default)."""
        return url

    # ------------------------------------------------------------------
    # BuildTransport surface (shared)
    # ------------------------------------------------------------------

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* on the host."""
        return self._run_remote(argv, cwd=cwd, timeout_s=timeout_s)

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text from the host.

        Decodes the bytes from :meth:`read_bytes` as UTF-8 rather than relying on the
        transport's locale-default decoding.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the content is not valid UTF-8.
        """
        raw = self.read_bytes(path)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CategorizedError(
                f"remote file {path!r} is not valid UTF-8",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": path},
            ) from exc

    def read_bytes(self, path: str) -> bytes:
        """Read *path* as raw bytes from the host (via ``base64 -w0``).

        The captured base64 output is size-capped (``_MAX_REMOTE_READ_B64_BYTES``) so a
        mis-pointed path cannot exhaust worker memory; these reads are small by design.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the remote read fails;
                ``CONFIGURATION_ERROR`` if the captured output exceeds the size cap.
        """
        result = self._run_remote(["base64", "-w0", path], cwd="/", timeout_s=30)
        if result.returncode != 0:
            raise CategorizedError(
                f"remote read_bytes failed for {path!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "path": path,
                    "stderr": redacted_tail(result.stderr, self._secret_registry),
                },
            )
        encoded = result.stdout.strip()
        if len(encoded) > _MAX_REMOTE_READ_B64_BYTES:
            raise CategorizedError(
                f"remote file {path!r} exceeds the maximum readable size",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": path, "max_b64_bytes": _MAX_REMOTE_READ_B64_BYTES},
            )
        return base64.b64decode(encoded)

    def clone(self, remote: str, ref: str, dest: str) -> None:
        """Clone *remote* at *ref* into *dest* using a shallow fetch.

        Validates *remote* and *ref* for control characters and leading dashes before issuing
        any host command. Uses ``git init`` + ``git fetch --depth 1`` + ``git checkout
        FETCH_HEAD`` to minimize data transferred (resolves an arbitrary ref/sha, which a plain
        ``clone --depth 1`` cannot).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe remote/ref, or when
                ``git checkout FETCH_HEAD`` exits non-zero.
        """
        _validate_git_arg(remote, "remote")
        _validate_git_arg(ref, "ref")

        self._run_remote(["git", "init", dest], cwd="/", timeout_s=_CLONE_TIMEOUT_S)
        self._run_remote(
            ["git", "-C", dest, "fetch", "--depth", "1", remote, ref],
            cwd="/",
            timeout_s=_CLONE_TIMEOUT_S,
        )
        result = self._run_remote(
            ["git", "-C", dest, "checkout", "FETCH_HEAD"], cwd="/", timeout_s=_CLONE_TIMEOUT_S
        )
        if result.returncode != 0:
            raise CategorizedError(
                "git checkout FETCH_HEAD failed on remote",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"stderr": redacted_tail(result.stderr, self._secret_registry)},
            )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* from the host to *presigned* URL via ``curl``; return the ETag.

        Runs ``curl -fsS -X PUT --upload-file <path> <url>`` with each required header via
        ``-H``, dumps the response headers to stdout (``-D -``), discards the body
        (``-o /dev/null``), and parses the ETag from the dumped headers.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the URL contains control characters;
                ``INFRASTRUCTURE_FAILURE`` when curl exits non-zero.
        """
        _validate_url(presigned.url)
        curl_argv = ["curl", "-fsS", "-X", "PUT", "--upload-file", path]
        for key, value in presigned.required_headers.items():
            curl_argv += ["-H", f"{key}: {value}"]
        curl_argv += ["-D", "-", "-o", "/dev/null", presigned.url]

        result = self._run_remote(curl_argv, cwd="/", timeout_s=_UPLOAD_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "remote curl PUT failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"url": self._upload_url_detail(presigned.url)},
            )
        return _extract_etag_from_headers(result.stdout).strip('"')

    def cleanup(self, path: str) -> None:
        """Remove *path* on the host (``rm -rf``); best-effort, logs on failure."""
        result = self._run_remote(["rm", "-rf", path], cwd="/", timeout_s=60)
        if result.returncode != 0:
            _log.warning("remote cleanup of %r failed (exit %d)", path, result.returncode)
