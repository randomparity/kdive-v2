"""`--version` prints and exits; every command logs the version at startup (ADR-0041)."""

from __future__ import annotations

import logging

import pytest

from kdive.__main__ import main


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("kdive ")


def test_startup_logs_version(monkeypatch, caplog):
    # Don't actually run the async loop; just confirm main logs before dispatching.
    # A runnable command now validates config at startup, so supply the one var the
    # reconciler requires (KDIVE_DATABASE_URL) so validation passes and dispatch is reached.
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://kdive@localhost/kdive")
    monkeypatch.setattr("kdive.__main__.asyncio.run", lambda coro: coro.close())
    # Capture on the emitting logger directly — if configure_logging() is later
    # changed to attach handlers to the kdive hierarchy directly (bypassing root),
    # caplog at root would miss it.
    with caplog.at_level("INFO", logger="kdive.__main__"):
        main(["reconciler"])
    assert any(
        "starting kdive" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records
    )
