"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.__main__ import build_parser
from kdive.security.secrets.secret_registry import SecretRegistry


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "INFO"


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"


def test_run_reconciler_builds_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_reconciler` opens a pool, constructs a Reconciler with NullReaper, runs, closes."""
    from kdive import __main__
    from kdive.providers import composition
    from kdive.reconciler import loop

    events: list[str] = []

    class _FakePool:
        async def open(self) -> None:
            events.append("open")

        async def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(__main__, "create_pool", lambda **kw: _FakePool())

    class _FakeResolver:
        async def register_all_discovery(self, pool: object) -> None:
            events.append("discover")

    monkeypatch.setattr(composition, "build_provider_resolver", lambda **kw: _FakeResolver())

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, reaper: object, **kw: object) -> None:
        constructed["reaper"] = reaper

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler(SecretRegistry()))

    assert events == ["open", "discover", "run", "close"]
    assert isinstance(constructed["reaper"], loop.NullReaper)
