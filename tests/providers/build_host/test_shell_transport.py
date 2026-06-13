"""Unit tests for the ShellBuildTransport base (ADR-0100).

The base implements the BuildTransport surface in terms of an abstract ``_run_remote``. A
tiny recording subclass drives it with no real host, so the shared
read/clone/upload/cleanup behavior is pinned independently of ssh or guest-exec.
"""

from __future__ import annotations

import base64

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.shell_transport import (
    _MAX_REMOTE_READ_B64_BYTES,
    ShellBuildTransport,
)
from kdive.providers.build_host.transport import CommandResult
from kdive.security.secrets.secret_registry import SecretRegistry


class _RecordingTransport(ShellBuildTransport):
    """A ShellBuildTransport whose ``_run_remote`` records calls and returns canned results."""

    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self._secret_registry = SecretRegistry()
        self.calls: list[tuple[list[str], str, int]] = []
        self._results = results or []

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        self.calls.append((argv, cwd, timeout_s))
        if self._results:
            return self._results.pop(0)
        return CommandResult(returncode=0, stdout="", stderr="")

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover - not under test
        raise NotImplementedError


def _ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def test_read_bytes_issues_base64_and_decodes() -> None:
    payload = b"\x00\x01\x02\xff data"
    t = _RecordingTransport([_ok(stdout=base64.b64encode(payload).decode())])
    assert t.read_bytes("/x.bin") == payload
    argv, cwd, _ = t.calls[0]
    assert argv == ["base64", "-w0", "/x.bin"]
    assert cwd == "/"


def test_read_text_decodes_utf8() -> None:
    text = "# café CONFIG_CRASH_DUMP=y\n"
    t = _RecordingTransport([_ok(stdout=base64.b64encode(text.encode()).decode())])
    assert t.read_text("/.config") == text


def test_read_text_invalid_utf8_is_configuration_error() -> None:
    t = _RecordingTransport([_ok(stdout=base64.b64encode(b"\x80\x81").decode())])
    with pytest.raises(CategorizedError) as exc:
        t.read_text("/.config")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR


def test_read_bytes_oversize_is_configuration_error() -> None:
    t = _RecordingTransport([_ok(stdout="A" * (_MAX_REMOTE_READ_B64_BYTES + 4))])
    with pytest.raises(CategorizedError) as exc:
        t.read_bytes("/huge.bin")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR


def test_read_bytes_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=1, stderr="No such file")])
    with pytest.raises(CategorizedError) as exc:
        t.read_bytes("/missing")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE


def test_clone_issues_init_fetch_checkout_in_order() -> None:
    t = _RecordingTransport([_ok(), _ok(), _ok()])
    t.clone("https://git.example/linux.git", "v6.9", "/src")
    argvs = [c[0] for c in t.calls]
    assert argvs[0] == ["git", "init", "/src"]
    assert argvs[1] == [
        "git",
        "-C",
        "/src",
        "fetch",
        "--depth",
        "1",
        "https://git.example/linux.git",
        "v6.9",
    ]
    assert argvs[2] == ["git", "-C", "/src", "checkout", "FETCH_HEAD"]


def test_clone_checkout_non_zero_is_configuration_error() -> None:
    t = _RecordingTransport([_ok(), _ok(), _ok(returncode=1, stderr="pathspec")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "remote,ref",
    [("-bad", "v6.9"), ("https://x/linux.git", "-v"), ("https://x/linux\n.git", "v6.9")],
)
def test_clone_rejects_unsafe_args_before_any_run(remote: str, ref: str) -> None:
    t = _RecordingTransport()
    with pytest.raises(CategorizedError) as exc:
        t.clone(remote, ref, "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert t.calls == []  # validated before any host command


def test_upload_file_builds_curl_and_parses_etag() -> None:
    presigned = PresignedUpload(
        url="https://s3.example/put",
        required_headers={"x-amz-checksum-sha256": "abc123"},
    )
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret
    headers = f'HTTP/1.1 200 OK\r\nETag: "{_EMPTY_MD5}"\r\n\r\n'
    t = _RecordingTransport([_ok(stdout=headers)])
    etag = t.upload_file("/build/bzImage", presigned)
    assert etag == _EMPTY_MD5
    argv, cwd, _ = t.calls[0]
    assert "curl" in argv and "--upload-file" in argv and presigned.url in argv
    assert "-H" in argv and "x-amz-checksum-sha256: abc123" in argv


def test_upload_file_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=22)])
    with pytest.raises(CategorizedError) as exc:
        t.upload_file("/build/bzImage", PresignedUpload(url="https://s3/p", required_headers={}))
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE


def test_cleanup_issues_rm_rf() -> None:
    t = _RecordingTransport([_ok()])
    t.cleanup("/build/scratch")
    assert t.calls[0][0] == ["rm", "-rf", "/build/scratch"]


def test_run_delegates_to_run_remote_with_cwd_and_timeout() -> None:
    t = _RecordingTransport([_ok(stdout="hi")])
    result = t.run(["make", "-j4"], cwd="/ws", timeout_s=99)
    assert result.stdout == "hi"
    assert t.calls[0] == (["make", "-j4"], "/ws", 99)
