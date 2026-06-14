"""SshBuildTransport: BuildTransport over SSH for remote build hosts (ADR-0342 §7.3).

All remote commands are executed via the SSH CLI (``ssh -i <identity> ... <host> <cmd>``).
The SSH identity (private key) is materialized into a per-op 0600 temp file from the
configured secrets root, then deleted on every exit path.

The shared BuildTransport surface (``run``/``read_*``/``clone``/``upload_file``/``cleanup``)
lives on :class:`~kdive.providers.build_host.shell_transport.ShellBuildTransport`; this module
provides only the ssh-specific ``_run_remote`` primitive, the stdin-streamed ``write_bytes``,
and the identity lifecycle.
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
from kdive.providers.build_host.execution import launch_failure

# Re-export the read-size cap (defined on the base) for callers/tests importing it here.
from kdive.providers.build_host.shell_transport import (
    _MAX_REMOTE_READ_B64_BYTES as _MAX_REMOTE_READ_B64_BYTES,
)
from kdive.providers.build_host.shell_transport import _UNSAFE_CHARS, ShellBuildTransport
from kdive.providers.build_host.workspace import redacted_tail
from kdive.providers.ports.build_transport import CommandResult
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend, secrets_root_from_env

__all__ = ["SshBuildTransport", "materialized_ssh_identity"]

_log = logging.getLogger(__name__)

_SSH_BASE_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


class SshBuildTransport(ShellBuildTransport):
    """Implements :class:`BuildTransport` over SSH (the ssh-specific seam of the shared base).

    All operations shell out to the remote host via ``ssh``. The SSH identity file is
    provided externally (use :func:`materialized_ssh_identity` + :meth:`from_host` to bind
    the full lifecycle).

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
    # ShellBuildTransport primitives
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

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Execute *argv* in *cwd* on the remote host via SSH; return a :class:`CommandResult`.

        Maps :class:`subprocess.TimeoutExpired` to ``BUILD_FAILURE`` and :class:`OSError`
        launch failures to :func:`~kdive.providers.build_host.execution.launch_failure`.
        """
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

    def check_reachable(self, *, timeout_s: int) -> bool:
        """Return whether a bare ``ssh <host> true`` succeeds (reachability probe, ADR-0103).

        Runs ``true`` directly via :meth:`_ssh_argv` (reusing ``-i identity``,
        ``BatchMode=yes``, ``StrictHostKeyChecking=accept-new``, ``ConnectTimeout=10``) with
        **no** ``cd <workspace>`` prefix — a reachability check tests the SSH hop only, not
        workspace existence. Every non-success outcome returns ``False`` rather than raising:
        a non-zero exit (the redacted stderr tail is logged at ``warning`` so a changed host
        key under ``accept-new`` is diagnosable), a timeout, or an ssh launch failure.

        Args:
            timeout_s: The subprocess timeout; set larger than ssh's own ``ConnectTimeout``
                so ssh's connect timeout is the binding signal and this is only a backstop.

        Returns:
            ``True`` iff ``ssh <host> true`` exited ``0``; ``False`` otherwise.
        """
        ssh_argv = self._ssh_argv("true")
        try:
            proc = subprocess.run(
                ssh_argv,
                timeout=timeout_s,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            _log.warning(
                "ssh reachability probe to %s timed out after %ss", self._address, timeout_s
            )
            return False
        except OSError:
            _log.warning("ssh reachability probe to %s could not launch ssh", self._address)
            return False
        if proc.returncode != 0:
            _log.warning(
                "ssh reachability probe to %s failed (rc=%d): %s",
                self._address,
                proc.returncode,
                redacted_tail(proc.stderr, self._secret_registry),
            )
            return False
        return True

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* on the remote host by piping base64-encoded content to stdin.

        Encodes *data* as base64, pipes it to ``base64 -d`` on the remote (via the ssh
        process stdin), and redirects output to *path*.
        """
        encoded = base64.b64encode(data).decode()
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
