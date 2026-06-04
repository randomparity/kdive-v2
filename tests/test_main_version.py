"""`--version` prints and exits; every command logs the version at startup (ADR-0041)."""

from __future__ import annotations

import pytest

from kdive.__main__ import main


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("kdive ")


def test_startup_logs_version(monkeypatch, caplog):
    # Don't actually run the async loop; just confirm main logs before dispatching.
    monkeypatch.setattr("kdive.__main__.asyncio.run", lambda coro: coro.close())
    # Capture on the emitting logger directly, so this does not depend on whether
    # configure_logging() leaves propagation to root enabled (ADR-0014 may set
    # propagate=False / replace root handlers — caplog at root would then miss it).
    with caplog.at_level("INFO", logger="kdive.__main__"):
        main(["reconciler"])
    assert any("starting kdive" in r.getMessage() for r in caplog.records)
