"""Unit tests for the constrained qemu-guest-agent exec primitive (issue #202, ADR-0078).

The primitive runs a worker-composed, allowlisted command in-guest via the
``guest-exec``/``guest-exec-status`` agent protocol over an injected ``agent_command``
callable (production: ``libvirt_qemu.qemuAgentCommand``); no real host is touched.
"""

from __future__ import annotations

import base64
import itertools
import json
from collections.abc import Callable
from typing import Any

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import GuestAgentExec

_ALLOWED = frozenset({"/usr/bin/curl", "/usr/bin/kdive-install"})


def _float_clock() -> Callable[[], float]:
    """A monotonic stub that advances 2.0s per call without ever exhausting."""
    counter = itertools.count(0, 2)
    return lambda: float(next(counter))


class _FakeAgent:
    """Scripts ``guest-exec``→pid then ``guest-exec-status``→exit for one in-guest run."""

    def __init__(
        self,
        *,
        exitcode: int | None = 0,
        signal: int | None = None,
        out: bytes = b"",
        err: bytes = b"",
        status_sequence: list[bool] | None = None,
    ) -> None:
        self._exitcode = exitcode
        self._signal = signal
        self._out = out
        self._err = err
        # Each False is a not-yet-exited poll before the final exited=True.
        self._status_sequence = list(status_sequence or [True])
        self.commands: list[dict[str, Any]] = []
        self.timeouts: list[int] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        parsed = json.loads(command)
        self.commands.append(parsed)
        self.timeouts.append(timeout)
        if parsed["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 4242}})
        if parsed["execute"] == "guest-exec-status":
            exited = self._status_sequence.pop(0) if self._status_sequence else True
            payload: dict[str, object] = {"exited": exited}
            if exited:
                # qemu-guest-agent reports exitcode on a normal exit OR signal on a kill.
                if self._signal is not None:
                    payload["signal"] = self._signal
                elif self._exitcode is not None:
                    payload["exitcode"] = self._exitcode
                if self._out:
                    payload["out-data"] = base64.b64encode(self._out).decode()
                if self._err:
                    payload["err-data"] = base64.b64encode(self._err).decode()
            return json.dumps({"return": payload})
        raise AssertionError(f"unexpected agent command {parsed!r}")


def _exec(agent: _FakeAgent) -> GuestAgentExec:
    return GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )


def test_run_returns_captured_stdout_and_exit_status() -> None:
    agent = _FakeAgent(exitcode=0, out=b"published-object-bytes")
    result = _exec(agent).run(object(), ["/usr/bin/curl", "-fsS", "https://store/obj"])
    assert result.exit_status == 0
    assert result.stdout == b"published-object-bytes"
    assert result.stderr == b""
    issued = [c["execute"] for c in agent.commands]
    assert issued == ["guest-exec", "guest-exec-status"]
    exec_args = agent.commands[0]["arguments"]
    assert exec_args["path"] == "/usr/bin/curl"
    assert exec_args["arg"] == ["-fsS", "https://store/obj"]
    assert exec_args["capture-output"] is True


def test_run_polls_until_the_command_exits() -> None:
    agent = _FakeAgent(out=b"done", status_sequence=[False, False, True])
    result = _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert result.stdout == b"done"
    assert [c["execute"] for c in agent.commands].count("guest-exec-status") == 3


def test_run_rejects_a_non_allowlisted_program() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(object(), ["/bin/sh", "-c", "curl https://store/obj"])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.commands == []  # rejected before any agent round-trip


def test_run_rejects_an_empty_argv() -> None:
    agent = _FakeAgent()
    with pytest.raises(CategorizedError) as excinfo:
        _exec(agent).run(object(), [])
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.commands == []


def test_agent_unreachable_maps_to_transport_failure() -> None:
    def boom(domain: object, command: str, timeout: int, flags: int) -> str:
        raise libvirt.libvirtError("guest agent is not connected")

    exc = GuestAgentExec(
        agent_command=boom,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_malformed_agent_response_maps_to_infrastructure_failure() -> None:
    def garbage(domain: object, command: str, timeout: int, flags: int) -> str:
        return "not json at all"

    exc = GuestAgentExec(
        agent_command=garbage,
        allowed_programs=_ALLOWED,
        sleep=lambda _s: None,
        monotonic=_float_clock(),
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_agent_calls_use_a_bounded_positive_timeout() -> None:
    # A blocking (-2) timeout would let a disconnected agent wedge the worker; each
    # call must carry a positive bound so the seam's deadline governs total time.
    agent = _FakeAgent(out=b"ok")
    _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert agent.timeouts  # at least one round-trip happened
    assert all(timeout > 0 for timeout in agent.timeouts)


def test_signal_killed_command_is_not_reported_as_success() -> None:
    # guest-exec-status returns `signal` (no exitcode) when the process is killed
    # (OOM, timeout-kill, SIGSEGV); defaulting a missing exitcode to 0 would read
    # a killed install as success.
    agent = _FakeAgent(exitcode=None, signal=9, out=b"partial")
    result = _exec(agent).run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert result.exit_status != 0
    assert result.exit_status == 128 + 9


def test_run_times_out_when_the_command_never_exits() -> None:
    agent = _FakeAgent(status_sequence=[False] * 50)
    exc = GuestAgentExec(
        agent_command=agent,
        allowed_programs=_ALLOWED,
        timeout_s=6.0,
        sleep=lambda _s: None,
        monotonic=iter([0.0, 2.0, 4.0, 6.0, 8.0]).__next__,
    )
    with pytest.raises(CategorizedError) as excinfo:
        exc.run(object(), ["/usr/bin/curl", "https://store/obj"])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
