"""Constrained qemu-guest-agent in-target exec over the qemu+tls connection (ADR-0078).

The in-target execution seam M2 realizes for the object-store artifact channel: the
worker composes a **constrained, allowlisted** command (never a shell string trusted to
the guest) and runs it in-guest through the guest agent's two-phase
``guest-exec``/``guest-exec-status`` protocol, capturing stdout/stderr. The agent
round-trip is an injected ``agent_command`` callable matching
``libvirt_qemu.qemuAgentCommand(domain, command, timeout, flags)`` so unit tests never
touch a real host; :func:`qemu_agent_command` is the production opener.

Enforcement is worker-side: ``argv[0]`` must be in the operator/worker-fixed
``allowed_programs`` set, so a later provider seam cannot smuggle an arbitrary program
into the guest. The TLS client cert is consumed by the libvirt transport layer and never
reaches this seam, so it cannot appear in a captured transcript (ADR-0077/0078).
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory

# qemuAgentCommand timeout sentinel: block until the agent answers one round-trip
# (libvirt's VIR_DOMAIN_QEMU_AGENT_COMMAND_BLOCK == -2). Overall command-exit timeout
# is owned by this seam's monotonic deadline, not the per-call agent timeout.
_AGENT_BLOCK = -2
_DEFAULT_TIMEOUT_S = 300.0
_DEFAULT_POLL_S = 1.0


type AgentCommand = Callable[[Any, str, int, int], str]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


class AgentExecResult(NamedTuple):
    """The captured result of one in-guest command run."""

    exit_status: int
    stdout: bytes
    stderr: bytes


def qemu_agent_command(domain: Any, command: str, timeout: int, flags: int) -> str:
    """Production opener: run a guest-agent command and return its JSON reply.

    Imported lazily so the package stays importable where the ``libvirt-qemu`` binding
    is absent (the same buildable-without-host posture as the rest of the provider).
    """
    import libvirt_qemu

    return libvirt_qemu.qemuAgentCommand(domain, command, timeout, flags)


def _decode_capture(payload: dict[str, Any], field: str) -> bytes:
    raw = payload.get(field)
    if raw is None:
        return b""
    try:
        return base64.b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise CategorizedError(
            f"guest agent returned an undecodable {field!r} capture",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


class GuestAgentExec:
    """Run worker-composed, allowlisted commands in-guest via the guest agent.

    All slow/host seams (the agent round-trip, the clock, sleep) are injected; unit
    tests drive the full two-phase protocol with no libvirt host.
    """

    def __init__(
        self,
        *,
        agent_command: AgentCommand,
        allowed_programs: frozenset[str],
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_s: float = _DEFAULT_POLL_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._agent_command = agent_command
        self._allowed_programs = allowed_programs
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    def run(self, domain: Any, argv: list[str]) -> AgentExecResult:
        """Run ``argv`` in-guest and return its captured stdout/stderr/exit status.

        ``argv[0]`` is the program path; the remainder are its arguments. The command
        is rejected before any agent round-trip unless ``argv[0]`` is allowlisted —
        enforcement is worker-side, never delegated to an in-guest shell.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an empty argv or a
                non-allowlisted program; ``TRANSPORT_FAILURE`` when the guest agent is
                unreachable or the command does not exit within the timeout;
                ``INFRASTRUCTURE_FAILURE`` for a malformed agent reply.
        """
        if not argv:
            raise CategorizedError(
                "guest-agent exec requires a non-empty argv",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        program, *args = argv
        if program not in self._allowed_programs:
            raise CategorizedError(
                f"guest-agent exec program {program!r} is not allowlisted",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"program": program},
            )
        pid = self._spawn(domain, program, args)
        return self._await_exit(domain, pid)

    def _spawn(self, domain: Any, program: str, args: list[str]) -> int:
        command = json.dumps(
            {
                "execute": "guest-exec",
                "arguments": {"path": program, "arg": args, "capture-output": True},
            }
        )
        reply = self._agent(domain, command)
        try:
            return int(reply["return"]["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CategorizedError(
                "guest agent did not return a pid for guest-exec",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc

    def _await_exit(self, domain: Any, pid: int) -> AgentExecResult:
        deadline = self._monotonic() + self._timeout_s
        status_command = json.dumps({"execute": "guest-exec-status", "arguments": {"pid": pid}})
        while True:
            reply = self._agent(domain, status_command)
            try:
                payload = reply["return"]
                exited = bool(payload["exited"])
            except (KeyError, TypeError) as exc:
                raise CategorizedError(
                    "guest agent returned a malformed guest-exec-status reply",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                ) from exc
            if exited:
                return AgentExecResult(
                    exit_status=int(payload.get("exitcode", 0)),
                    stdout=_decode_capture(payload, "out-data"),
                    stderr=_decode_capture(payload, "err-data"),
                )
            if self._monotonic() >= deadline:
                raise CategorizedError(
                    f"in-guest command did not exit within {self._timeout_s:g}s",
                    category=ErrorCategory.TRANSPORT_FAILURE,
                    details={"domain": _domain_name(domain), "timeout_s": self._timeout_s},
                )
            self._sleep(self._poll_s)

    def _agent(self, domain: Any, command: str) -> dict[str, Any]:
        try:
            raw = self._agent_command(domain, command, _AGENT_BLOCK, 0)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "qemu-guest-agent command failed (agent unreachable or not connected)",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"domain": _domain_name(domain)},
            ) from exc
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CategorizedError(
                "guest agent returned a non-JSON reply",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if not isinstance(decoded, dict):
            raise CategorizedError(
                "guest agent reply was not a JSON object",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        return decoded


def _domain_name(domain: Any) -> str:
    try:
        return domain.name()
    except (libvirt.libvirtError, AttributeError):
        return "<unknown>"
