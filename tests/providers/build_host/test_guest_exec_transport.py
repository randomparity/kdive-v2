"""Unit tests for GuestExecBuildTransport (ADR-0100).

A fake ``agent_command`` drives the two-phase guest-exec/guest-exec-status protocol with no
libvirt host. The transport composes one ``/bin/sh -c "cd <cwd> && exec <argv>"`` hop per
command (the sibling SSH posture), reuses the ShellBuildTransport base for read/clone/upload,
and registers the presigned URL for redaction before an in-guest curl.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.guest_exec_transport import GuestExecBuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry


class _FakeAgent:
    """Two-phase guest-agent fake: records spawned argvs; returns canned exit/out/err.

    ``never_exits`` makes guest-exec-status always report not-exited (drives the timeout path).
    """

    def __init__(
        self,
        *,
        exitcode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        never_exits: bool = False,
    ) -> None:
        self.spawned: list[dict[str, Any]] = []
        self._exitcode = exitcode
        self._stdout = stdout
        self._stderr = stderr
        self._never_exits = never_exits

    def __call__(self, domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            self.spawned.append(msg["arguments"])
            return json.dumps({"return": {"pid": 4321}})
        # guest-exec-status
        if self._never_exits:
            return json.dumps({"return": {"exited": False}})
        return json.dumps(
            {
                "return": {
                    "exited": True,
                    "exitcode": self._exitcode,
                    "out-data": base64.b64encode(self._stdout).decode(),
                    "err-data": base64.b64encode(self._stderr).decode(),
                }
            }
        )


def _transport(
    agent: _FakeAgent, *, registry: SecretRegistry | None = None
) -> GuestExecBuildTransport:
    return GuestExecBuildTransport(
        domain=object(),
        agent_command=agent,
        secret_registry=registry or SecretRegistry(),
        poll_s=0.0,
        sleep=lambda _s: None,
        monotonic=_clock(),
    )


def _clock() -> Any:
    """A monotonic that advances 1s per call (so a never-exits poll loop hits its deadline)."""
    ticks = iter(range(0, 100000))
    return lambda: float(next(ticks))


def test_run_composes_single_sh_c_hop() -> None:
    agent = _FakeAgent(exitcode=0, stdout=b"ok")
    result = _transport(agent).run(["make", "-C", "/ws", "x"], cwd="/ws", timeout_s=60)
    assert result.returncode == 0
    assert result.stdout == "ok"
    args = agent.spawned[0]
    assert args["path"] == "/bin/sh"
    assert args["arg"] == ["-c", "cd /ws && exec make -C /ws x"]


def test_run_quotes_cwd_and_argv() -> None:
    agent = _FakeAgent()
    _transport(agent).run(["echo", "a b"], cwd="/has space", timeout_s=10)
    assert agent.spawned[0]["arg"] == ["-c", "cd '/has space' && exec echo 'a b'"]


def test_run_non_zero_exit_is_returned_not_raised() -> None:
    agent = _FakeAgent(exitcode=2, stderr=b"boom")
    result = _transport(agent).run(["make", "-C", "/ws"], cwd="/ws", timeout_s=10)
    assert result.returncode == 2
    assert result.stderr == "boom"


def test_run_timeout_maps_to_transport_failure() -> None:
    agent = _FakeAgent(never_exits=True)
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).run(["make"], cwd="/ws", timeout_s=3)
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE


def test_read_bytes_round_trips_via_base64() -> None:
    payload = b"\x00\x01config\xff"
    agent = _FakeAgent(stdout=base64.b64encode(payload))
    # read_bytes runs `base64 -w0 <path>`; the fake returns its stdout as the b64 of payload.
    result = _transport(agent).read_bytes("/build/.config")
    assert result == payload
    assert agent.spawned[0]["arg"] == ["-c", "cd / && exec base64 -w0 /build/.config"]


def test_write_bytes_composes_pipeline_not_exec() -> None:
    data = b"\x00config\xff"
    agent = _FakeAgent(exitcode=0)
    _transport(agent).write_bytes("/build/dest.bin", data)
    encoded = base64.b64encode(data).decode()
    arg = agent.spawned[0]["arg"]
    assert arg[0] == "-c"
    # The pipeline must NOT be wrapped in the exec-join form (a pipe cannot be exec'd).
    assert arg[1] == f"printf %s {encoded} | base64 -d > /build/dest.bin"
    assert "exec" not in arg[1]


def test_write_bytes_non_zero_is_infrastructure_failure() -> None:
    agent = _FakeAgent(exitcode=1, stderr=b"No space left")
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).write_bytes("/build/dest.bin", b"x")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE


def test_clone_runs_init_fetch_checkout_via_agent() -> None:
    agent = _FakeAgent(exitcode=0)
    _transport(agent).clone("https://git.example/linux.git", "v6.9", "/src")
    cmds = [s["arg"][1] for s in agent.spawned]
    assert cmds[0] == "cd / && exec git init /src"
    assert "fetch --depth 1 https://git.example/linux.git v6.9" in cmds[1]
    assert cmds[2] == "cd / && exec git -C /src checkout FETCH_HEAD"


def test_upload_file_registers_url_before_exec_and_redacts_on_failure() -> None:
    registry = SecretRegistry()
    presigned = PresignedUpload(
        url="https://s3.example/put?X-Amz-Signature=secretsig",
        required_headers={"x-amz-checksum-sha256": "abc"},
    )
    agent = _FakeAgent(exitcode=22)  # curl failure
    with pytest.raises(CategorizedError) as exc:
        _transport(agent, registry=registry).upload_file("/build/bzImage", presigned)
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    # The URL was registered for redaction (so any transcript masks it).
    assert presigned.url in registry.snapshot()
    # The error detail carries only the query-stripped URL, never the live signature.
    assert "secretsig" not in str(exc.value.details)


def test_upload_file_success_parses_etag() -> None:
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret
    headers = f'HTTP/1.1 200 OK\r\nETag: "{_EMPTY_MD5}"\r\n\r\n'
    agent = _FakeAgent(exitcode=0, stdout=headers.encode())
    etag = _transport(agent).upload_file(
        "/build/bzImage", PresignedUpload(url="https://s3/p", required_headers={})
    )
    assert etag == _EMPTY_MD5
