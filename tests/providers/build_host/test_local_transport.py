"""Unit tests for LocalBuildTransport (Task 5 — transport seam, ADR-0342)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.transport import LocalBuildTransport
from kdive.providers.ports.build_transport import CommandResult

_RUN_TARGET = "kdive.providers.build_host.transport.subprocess.run"

# ---------------------------------------------------------------------------
# 1. run — argv and call-shape preservation
# ---------------------------------------------------------------------------


def test_run_argv_preserved() -> None:
    """run() forwards argv, cwd, timeout unchanged to subprocess.run."""
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "done\n"
    fake_result.stderr = ""

    with patch(_RUN_TARGET, return_value=fake_result) as mock_run:
        transport = LocalBuildTransport()
        result = transport.run(["make", "-C", "/ws", "-j4"], cwd="/ws", timeout_s=100)

    mock_run.assert_called_once_with(
        ["make", "-C", "/ws", "-j4"],
        cwd="/ws",
        timeout=100,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result == CommandResult(returncode=0, stdout="done\n", stderr="")


# ---------------------------------------------------------------------------
# 2. run — launch failure maps to MISSING_DEPENDENCY via launch_failure
# ---------------------------------------------------------------------------


def test_run_file_not_found_maps_to_missing_dependency() -> None:
    """run() surfaces FileNotFoundError as MISSING_DEPENDENCY CategorizedError."""
    with patch(_RUN_TARGET, side_effect=FileNotFoundError("no such file")):
        transport = LocalBuildTransport()
        with pytest.raises(CategorizedError) as exc_info:
            transport.run(["make", "-C", "/ws"], cwd="/ws", timeout_s=60)

    assert exc_info.value.category == ErrorCategory.MISSING_DEPENDENCY


def test_run_timeout_maps_to_build_failure() -> None:
    """run() surfaces subprocess.TimeoutExpired as a BUILD_FAILURE CategorizedError."""
    timeout = subprocess.TimeoutExpired(cmd=["make"], timeout=60)
    with patch(_RUN_TARGET, side_effect=timeout):
        transport = LocalBuildTransport()
        with pytest.raises(CategorizedError) as exc_info:
            transport.run(["make", "-C", "/ws"], cwd="/ws", timeout_s=60)

    assert exc_info.value.category == ErrorCategory.BUILD_FAILURE


# ---------------------------------------------------------------------------
# 3. clone — raises CONFIGURATION_ERROR (local builds use warm tree, not git)
# ---------------------------------------------------------------------------


def test_clone_raises_configuration_error() -> None:
    """clone() raises CategorizedError with CONFIGURATION_ERROR."""
    transport = LocalBuildTransport()
    with pytest.raises(CategorizedError) as exc_info:
        transport.clone("https://git.kernel.org/pub/scm/linux.git", "v6.9", "/dest")

    assert exc_info.value.category == ErrorCategory.CONFIGURATION_ERROR


# ---------------------------------------------------------------------------
# 4. read_text / read_bytes / write_bytes — round-trip via real filesystem
# ---------------------------------------------------------------------------


def test_read_write_round_trip(tmp_path: Path) -> None:
    """write_bytes then read_bytes round-trips binary data through the real filesystem."""
    transport = LocalBuildTransport()
    target = tmp_path / "payload.bin"

    data = b"\x00\x01\x02\x03hello"
    transport.write_bytes(str(target), data)

    assert transport.read_bytes(str(target)) == data


def test_read_text_returns_file_content(tmp_path: Path) -> None:
    """read_text returns the text content written by Path.write_text."""
    p = tmp_path / "config.txt"
    p.write_text("CONFIG_CRASH_DUMP=y\n")
    transport = LocalBuildTransport()
    assert transport.read_text(str(p)) == "CONFIG_CRASH_DUMP=y\n"


def test_read_bytes_returns_file_bytes(tmp_path: Path) -> None:
    """read_bytes returns the raw bytes written to disk."""
    p = tmp_path / "bzImage"
    p.write_bytes(b"\x7fELF")
    transport = LocalBuildTransport()
    assert transport.read_bytes(str(p)) == b"\x7fELF"


def test_write_bytes_creates_file(tmp_path: Path) -> None:
    """write_bytes creates (or overwrites) a file with the given bytes."""
    p = tmp_path / "out.bin"
    transport = LocalBuildTransport()
    transport.write_bytes(str(p), b"hello")
    assert p.read_bytes() == b"hello"


# ---------------------------------------------------------------------------
# 5. upload_file — injected http_put called with correct args; ETag stripped
# ---------------------------------------------------------------------------


def test_upload_file_calls_http_put_and_strips_etag(tmp_path: Path) -> None:
    """upload_file reads the file, calls http_put with (url, data, headers), strips etag quotes."""
    payload = b"kernel-image-bytes"
    f = tmp_path / "bzImage"
    f.write_bytes(payload)

    calls: list[tuple[str, bytes, dict[str, str]]] = []

    def fake_put(url: str, data: bytes, headers: dict[str, str]) -> str:
        calls.append((url, data, headers))
        return '"abc123"'

    presigned = PresignedUpload(
        url="https://s3.example.com/put",
        required_headers={"x-amz-checksum": "sha256val"},
    )
    transport = LocalBuildTransport(http_put=fake_put)
    etag = transport.upload_file(str(f), presigned)

    assert len(calls) == 1
    url, data, headers = calls[0]
    assert url == "https://s3.example.com/put"
    assert data == payload
    assert headers == {"x-amz-checksum": "sha256val"}
    assert etag == "abc123"  # quotes stripped


# ---------------------------------------------------------------------------
# 6. cleanup — removes a directory tree, unlinks a file, no-ops on a missing path
# ---------------------------------------------------------------------------


def test_cleanup_removes_directory_unlinks_file_and_noops_missing(tmp_path: Path) -> None:
    """cleanup removes a dir tree, unlinks a file, and is a no-op for a missing path."""
    transport = LocalBuildTransport()

    workspace = tmp_path / "workspace"
    (workspace / "nested").mkdir(parents=True)
    (workspace / "nested" / "file.txt").write_text("x")
    transport.cleanup(str(workspace))
    assert not workspace.exists()

    artifact = tmp_path / "bzImage"
    artifact.write_bytes(b"\x7fELF")
    transport.cleanup(str(artifact))
    assert not artifact.exists()

    transport.cleanup(str(tmp_path / "never-existed"))  # harmless no-op
