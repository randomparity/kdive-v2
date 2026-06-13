"""SshBuildTransport: BuildTransport over SSH for remote build hosts (ADR-0342 §7.3).

All remote commands are executed via the SSH CLI (``ssh -i <identity> ... <host> <cmd>``).
The SSH identity (private key) is materialized into a per-op 0600 temp file from the
configured secrets root, then deleted on every exit path.

``clone()`` performs a shallow checkout via ``git init`` + ``git fetch --depth 1`` +
``git checkout FETCH_HEAD``; a non-shallow fallback is a future improvement since some
remotes refuse shallow-by-SHA fetches.
"""

from __future__ import annotations

import base64
import logging
import os
import shlex
import subprocess  # noqa: S404 — fixed argv only, no shell=True
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kdive.db.build_hosts import BuildHost
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.execution import launch_failure
from kdive.providers.build_host.transport import CommandResult
from kdive.providers.build_host.workspace import redacted_tail
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend, secrets_root_from_env

_log = logging.getLogger(__name__)

# Characters that would be mis-parsed by git or ssh as options/command boundaries.
_UNSAFE_CHARS = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
)

_SSH_BASE_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
]

# read_bytes/read_text base64-capture a whole remote file into memory. These reads
# are small (.config, the build-id note) — cap the captured (base64) output well
# above any legitimate value so a mis-pointed path cannot exhaust worker memory.
_MAX_REMOTE_READ_B64_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


def _validate_ssh_destination(address: str) -> None:
    """Reject an ssh destination that ssh would parse as an option or that injects a command.

    An address beginning with ``-`` (e.g. ``-oProxyCommand=...``) is interpreted by ssh as
    an option rather than a host, which is arbitrary command execution; control characters or
    newlines could split the argv. Validated at construction so every code path is covered.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the address starts with ``-`` or
            contains a control character or newline.
    """
    if address.startswith("-"):
        raise CategorizedError(
            "ssh address must not start with '-' (would be parsed as an ssh option)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "address"},
        )
    if any(c in _UNSAFE_CHARS for c in address):
        raise CategorizedError(
            "ssh address contains a control character or newline",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "address"},
        )


def _write_private(path: Path, value: str) -> None:
    """Write *value* to *path* with mode 0600 (exclusive create, no umask interference)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)


def _resolve_ssh_key(
    credential_ref: str, secret_registry: SecretRegistry, scope: object | None = None
) -> str:
    """Resolve *credential_ref* against the secrets root, register with *secret_registry*.

    Uses :class:`~kdive.security.secrets.secrets.FileRefBackend` confined to the
    ``KDIVE_SECRETS_ROOT`` — the same mechanism :mod:`kdive.providers.remote_libvirt.transport`
    uses for TLS material. The value is registered into ``secret_registry`` under ``scope``
    before being returned.

    Args:
        credential_ref: A root-relative secret file path (e.g. ``"build_host_key.pem"``).
        secret_registry: Registry to register the resolved value into for redaction.
        scope: Optional scope for :meth:`SecretRegistry.register`; ``None`` registers globally.

    Returns:
        The private key PEM contents (newline-stripped trailing whitespace).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the ref cannot be resolved (missing,
            path-escape, oversized).
    """
    from kdive.security.secrets.paths import PathSafetyError

    backend = FileRefBackend(secrets_root_from_env(), secret_registry, scope=scope)
    try:
        return backend.resolve(credential_ref)
    except PathSafetyError as exc:
        raise CategorizedError(
            f"ssh credential ref {credential_ref!r} could not be resolved: {exc}",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc


# ---------------------------------------------------------------------------
# materialized_ssh_identity context manager
# ---------------------------------------------------------------------------


@contextmanager
def materialized_ssh_identity(
    ssh_credential_ref: str,
    secret_registry: SecretRegistry,
    *,
    scope: object | None = None,
) -> Iterator[Path]:
    """Materialize a private SSH identity file for one operation; delete on every exit.

    Resolves ``ssh_credential_ref`` via the secrets root, registers the key value with
    ``secret_registry`` for redaction coverage (ADR-0027), writes a 0600 temp file, yields
    its :class:`~pathlib.Path`, then unconditionally deletes it in ``finally``.

    Args:
        ssh_credential_ref: Root-relative secret file path for the SSH private key.
        secret_registry: Registry to register the key value into.
        scope: Optional scope for registry registration.

    Yields:
        Path to the materialized identity file (mode 0600, unlinked after the block).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the ref cannot be resolved.
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the temp file cannot be created.
    """
    key_value = _resolve_ssh_key(ssh_credential_ref, secret_registry, scope=scope)
    # Register AFTER resolution so the value is masked even if _resolve_ssh_key was
    # patched in tests (and thus did not call FileRefBackend.resolve's own registration).
    secret_registry.register(key_value, scope=scope)
    try:
        fd, tmp_path_str = tempfile.mkstemp(prefix="kdive-ssh-identity-", suffix=".pem")
        os.close(fd)
        identity_path = Path(tmp_path_str)
        # Replace the mkstemp file (0600 by default on Linux but not guaranteed) with
        # an exclusive O_CREAT write at exactly 0600.
        identity_path.unlink()
        _write_private(identity_path, key_value)
    except OSError as exc:
        raise CategorizedError(
            f"could not materialize SSH identity file: {exc}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    try:
        yield identity_path
    finally:
        try:
            identity_path.unlink()
        except OSError:
            _log.exception(
                "failed to delete SSH identity file %s; private key material may remain on disk",
                identity_path,
            )


# ---------------------------------------------------------------------------
# SshBuildTransport
# ---------------------------------------------------------------------------


class SshBuildTransport:
    """Implements :class:`~kdive.providers.build_host.transport.BuildTransport` over SSH.

    All operations shell out to the remote host via ``ssh``. The SSH identity file is
    provided externally (use :func:`materialized_ssh_identity` + :meth:`from_host` to
    bind the full lifecycle).

    Args:
        address: SSH destination in ``[user@]host`` form.
        identity_path: Path to the materialized 0600 private-key file.
        secret_registry: Registry for redacting secrets from error details.
    """

    def __init__(
        self,
        *,
        address: str,
        identity_path: Path,
        secret_registry: SecretRegistry,
    ) -> None:
        _validate_ssh_destination(address)
        self._address = address
        self._identity_path = identity_path
        self._secret_registry = secret_registry

    @classmethod
    @contextmanager
    def from_host(
        cls,
        host: BuildHost,
        secret_registry: SecretRegistry,
    ) -> Iterator[SshBuildTransport]:
        """Context manager that materializes the SSH identity and yields a ready transport.

        Enters :func:`materialized_ssh_identity` for ``host.ssh_credential_ref``, constructs
        a :class:`SshBuildTransport` bound to ``host.address``, yields it, then removes the
        identity file on exit.

        Args:
            host: A :class:`~kdive.db.build_hosts.BuildHost` row with ``kind == "ssh"``
                (``address`` and ``ssh_credential_ref`` must be non-None).
            secret_registry: Registry passed through to :func:`materialized_ssh_identity`.

        Yields:
            A ready :class:`SshBuildTransport` with a live identity file.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when ``address`` or
                ``ssh_credential_ref`` is absent (host is not an SSH host).
        """
        if host.address is None or host.ssh_credential_ref is None:
            raise CategorizedError(
                f"build host {host.name!r} is not an SSH host (missing address or credential ref)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"host": host.name},
            )
        with materialized_ssh_identity(host.ssh_credential_ref, secret_registry) as identity_path:
            yield cls(
                address=host.address,
                identity_path=identity_path,
                secret_registry=secret_registry,
            )

    # ------------------------------------------------------------------
    # Internal ssh runner
    # ------------------------------------------------------------------

    def _ssh_argv(self, remote_cmd: str) -> list[str]:
        """Build the ssh argv for a single remote command string."""
        return [
            "ssh",
            "-i",
            str(self._identity_path),
            *_SSH_BASE_OPTIONS,
            self._address,
            remote_cmd,
        ]

    def _run_remote(self, argv: list[str], cwd: str, timeout_s: int) -> CommandResult:
        """Execute *argv* in *cwd* on the remote host; return a :class:`CommandResult`."""
        remote_cmd = f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"
        ssh_argv = self._ssh_argv(remote_cmd)
        try:
            proc = subprocess.run(
                ssh_argv,
                timeout=timeout_s,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "ssh command exceeded the build timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": timeout_s},
            ) from exc
        except OSError as exc:
            raise launch_failure("ssh", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    # ------------------------------------------------------------------
    # BuildTransport protocol
    # ------------------------------------------------------------------

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* on the remote host via SSH.

        Maps :class:`subprocess.TimeoutExpired` to ``BUILD_FAILURE`` and
        :class:`OSError` launch failures to
        :func:`~kdive.providers.build_host.execution.launch_failure`.
        """
        return self._run_remote(argv, cwd, timeout_s)

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text from the remote host.

        Decodes the bytes from :meth:`read_bytes` as UTF-8 rather than relying on the ssh
        subprocess's locale-default text decoding, satisfying the Protocol's UTF-8 contract.

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
        """Read *path* as raw bytes from the remote host (via ``base64 -w0``).

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

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* on the remote host by piping base64-encoded content.

        Encodes *data* as base64, pipes it to ``base64 -d`` on the remote, and redirects
        output to *path*. Uses a shell heredoc-free approach: the base64 payload is
        written to the ssh process stdin via a second ``subprocess.run`` call piping stdin
        directly.
        """
        encoded = base64.b64encode(data).decode()
        # Construct a remote command that decodes base64 from stdin and writes to path.
        remote_cmd = f"base64 -d > {shlex.quote(path)}"
        ssh_argv = self._ssh_argv(remote_cmd)
        try:
            proc = subprocess.run(
                ssh_argv,
                input=encoded,
                timeout=60,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "remote write_bytes exceeded the timeout",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        except OSError as exc:
            raise launch_failure("ssh", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
        if proc.returncode != 0:
            raise CategorizedError(
                f"remote write_bytes failed for {path!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"path": path, "stderr": redacted_tail(proc.stderr, self._secret_registry)},
            )

    def clone(self, remote: str, ref: str, dest: str) -> None:
        """Clone *remote* at *ref* into *dest* using a shallow fetch.

        Validates *remote* and *ref* for control characters and leading dashes before
        issuing any subprocess call. Uses ``git init`` + ``git fetch --depth 1`` +
        ``git checkout FETCH_HEAD`` to minimize data transferred.

        Note: Shallow-by-SHA fetches may be refused by some remotes; a non-shallow
        fallback is a future improvement.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe remote/ref, or when
                ``git checkout FETCH_HEAD`` exits non-zero.
        """
        _validate_git_arg(remote, "remote")
        _validate_git_arg(ref, "ref")

        timeout_s = 10 * 60  # 10 minutes for clone operations

        # Step 1: git init <dest>
        self._run_remote(["git", "init", dest], cwd="/", timeout_s=timeout_s)

        # Step 2: git -C <dest> fetch --depth 1 <remote> <ref>
        self._run_remote(
            ["git", "-C", dest, "fetch", "--depth", "1", remote, ref],
            cwd="/",
            timeout_s=timeout_s,
        )

        # Step 3: git -C <dest> checkout FETCH_HEAD
        result = self._run_remote(
            ["git", "-C", dest, "checkout", "FETCH_HEAD"],
            cwd="/",
            timeout_s=timeout_s,
        )
        if result.returncode != 0:
            raise CategorizedError(
                "git checkout FETCH_HEAD failed on remote",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"stderr": redacted_tail(result.stderr, self._secret_registry)},
            )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* on the remote host to *presigned* URL; return the ETag (quotes stripped).

        Runs ``curl -fsS -X PUT --upload-file <path> <url>`` with each required header via
        ``-H``, dumps the response headers to stdout (``-D -``), discards the body
        (``-o /dev/null``), and parses the ETag from the dumped headers.

        Args:
            path: Remote filesystem path to the file to upload.
            presigned: Presigned PUT URL and required headers.

        Returns:
            The ETag value with surrounding quotes removed.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the URL contains control characters.
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when curl exits non-zero.
        """
        _validate_git_arg_url(presigned.url)

        curl_argv = ["curl", "-fsS", "-X", "PUT", "--upload-file", path]
        for key, value in presigned.required_headers.items():
            curl_argv += ["-H", f"{key}: {value}"]
        # -D - dumps response headers to stdout; -o /dev/null discards the body.
        curl_argv += ["-D", "-", "-o", "/dev/null", presigned.url]

        result = self._run_remote(curl_argv, cwd="/", timeout_s=5 * 60)
        if result.returncode != 0:
            raise CategorizedError(
                "remote curl PUT failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"url": presigned.url},
            )
        etag = _extract_etag_from_headers(result.stdout)
        return etag.strip('"')

    def cleanup(self, path: str) -> None:
        """Remove *path* on the remote host (``rm -rf``); best-effort, ignores failure."""
        result = self._run_remote(["rm", "-rf", path], cwd="/", timeout_s=60)
        if result.returncode != 0:
            _log.warning("remote cleanup of %r failed (exit %d)", path, result.returncode)


# ---------------------------------------------------------------------------
# URL validation and header parsing helpers
# ---------------------------------------------------------------------------


def _validate_git_arg_url(url: str) -> None:
    """Reject a URL containing control characters.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe URL.
    """
    if any(c in _UNSAFE_CHARS for c in url):
        raise CategorizedError(
            "presigned URL contains a control character",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _extract_etag_from_headers(header_dump: str) -> str:
    """Parse the ETag value from a curl ``-D -`` header dump.

    Returns an empty string if the ETag header is absent (caller strips quotes and
    stores as-is; the worker HEADs the object afterward for verification).
    """
    for line in header_dump.splitlines():
        if line.lower().startswith("etag:"):
            return line.split(":", 1)[1].strip()
    return ""
