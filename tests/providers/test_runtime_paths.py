"""Provider runtime path helper tests."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.runtime_paths import console_log_path, domain_name_for, read_console_log

_SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_domain_name_for_uses_kdive_prefix() -> None:
    assert domain_name_for(_SYSTEM_ID) == "kdive-11111111-1111-1111-1111-111111111111"


def test_console_log_path_uses_provider_console_directory() -> None:
    assert console_log_path(_SYSTEM_ID) == Path(
        "/var/lib/kdive/console/11111111-1111-1111-1111-111111111111.log"
    )


def test_read_console_log_returns_existing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "console.log"
    path.write_bytes(b"booted\n")

    assert read_console_log(path) == b"booted\n"


def test_read_console_log_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_console_log(tmp_path / "missing.log") == b""


def test_read_console_log_permission_failure_is_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "console.log"

    def fail_read_bytes(self: Path) -> bytes:
        assert self == path
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(CategorizedError) as caught:
        read_console_log(path)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {
        "operation": "read_console_log",
        "path": str(path),
        "error": "PermissionError",
    }


def test_read_console_log_other_oserror_is_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "console.log"

    def fail_read_bytes(self: Path) -> bytes:
        assert self == path
        raise OSError("short read")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(CategorizedError) as caught:
        read_console_log(path)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert caught.value.details == {
        "operation": "read_console_log",
        "path": str(path),
        "error": "OSError",
    }
