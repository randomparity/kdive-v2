"""GuestExecBuildTransport: BuildTransport over the qemu-guest-agent exec channel (ADR-0100).

The ephemeral remote-libvirt build VM runs its build over the in-guest guest-agent exec
channel rather than SSH. ``guest-exec`` has no working-directory argument, so each command is
composed as a single in-guest shell hop — ``/bin/sh -c "cd <cwd> && exec <argv>"`` — exactly
the posture the sibling :class:`~kdive.providers.build_host.ssh_transport.SshBuildTransport`
uses (ssh runs the command through the remote login shell). The worker composes fixed,
``shlex``-quoted argv, so there is no injection surface; the build VM is an ephemeral,
single-build, operator-staged target (the divergence from ADR-0078's debug-target no-shell
rule is recorded in ADR-0100).

The shared read/clone/upload/cleanup surface comes from
:class:`~kdive.providers.build_host.shell_transport.ShellBuildTransport`; this module provides
the guest-agent ``_run_remote`` primitive and a ``write_bytes`` that composes its own base64
pipeline (which the ``exec``-join run form cannot express).
"""

from __future__ import annotations

import base64
import shlex
import time
from typing import Any

from kdive.diagnostics.egress_probe import redact_presigned
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.shell_transport import ShellBuildTransport
from kdive.providers.build_host.workspace import redacted_tail
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    GuestAgentExec,
    Monotonic,
    Sleep,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_SHELL = "/bin/sh"
_WRITE_TIMEOUT_S = 60


class GuestExecBuildTransport(ShellBuildTransport):
    """Implements :class:`BuildTransport` over the qemu-guest-agent exec channel.

    Each command is one ``/bin/sh -c`` hop through :class:`GuestAgentExec` (allowlist
    ``{'/bin/sh'}``). The agent's command-exit timeout is fixed at construction, so a
    fresh :class:`GuestAgentExec` is built per call with that call's ``timeout_s`` (a ``make``
    may run for hours while a ``.config`` read is seconds).

    Args:
        domain: The libvirt domain handle the guest-agent commands run against.
        agent_command: The guest-agent round-trip callable (production opener
            :func:`~kdive.providers.remote_libvirt.guest.agent.qemu_agent_command`).
        secret_registry: Registry for redacting secrets from error details and transcripts.
        poll_s: guest-exec-status poll interval.
        sleep: Injected sleep (tests pass a no-op).
        monotonic: Injected monotonic clock (tests pass a fake).
    """

    def __init__(
        self,
        *,
        domain: Any,
        agent_command: AgentCommand,
        secret_registry: SecretRegistry,
        poll_s: float = 1.0,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._domain = domain
        self._agent_command = agent_command
        self._secret_registry = secret_registry
        self._poll_s = poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    def _agent_for(self, timeout_s: int) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_SHELL}),
            timeout_s=float(timeout_s),
            poll_s=self._poll_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _exec_shell(self, command: str, timeout_s: int) -> CommandResult:
        """Run a single ``/bin/sh -c <command>`` hop in-guest and map it to a CommandResult."""
        result = self._agent_for(timeout_s).run(self._domain, [_SHELL, "-c", command])
        return CommandResult(
            returncode=result.exit_status,
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
        )

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* in-guest as one ``cd <cwd> && exec <argv>`` shell hop."""
        return self._exec_shell(f"cd {shlex.quote(cwd)} && exec {shlex.join(argv)}", timeout_s)

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* in-guest via a ``printf … | base64 -d`` pipeline.

        A pipe cannot be ``exec``-ed (the generic :meth:`_run_remote` form), so this composes
        its own shell command. The base64 payload is alphanumeric + ``+/=`` (no shell
        metacharacters) and the path is ``shlex``-quoted; the only writes are the small config
        fragment and patch bytes.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the in-guest write exits non-zero.
        """
        encoded = base64.b64encode(data).decode("ascii")
        command = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        result = self._exec_shell(command, _WRITE_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                f"remote write_bytes failed for {path!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "path": path,
                    "stderr": redacted_tail(result.stderr, self._secret_registry),
                },
            )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Register the presigned URL for redaction, then upload via the shared curl path.

        The URL is a bearer capability: registering it before the in-guest ``curl`` masks it in
        any captured transcript, and :meth:`_upload_url_detail` keeps the query-stripped form in
        error details (never the live signature).
        """
        self._secret_registry.register(presigned.url, scope=None)
        return super().upload_file(path, presigned)

    def _upload_url_detail(self, url: str) -> str:
        return redact_presigned(url)
