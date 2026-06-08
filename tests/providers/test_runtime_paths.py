"""Provider runtime path helper tests."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

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
