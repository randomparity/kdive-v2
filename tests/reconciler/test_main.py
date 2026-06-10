"""CLI wiring for the `python -m kdive reconciler` subcommand (issue #12)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.__main__ import build_parser
from kdive.security.secrets.secret_registry import SecretRegistry


def test_reconciler_subcommand_parses() -> None:
    args = build_parser().parse_args(["reconciler"])
    assert args.command == "reconciler"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_reconciler_subcommand_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "reconciler"])
    assert args.command == "reconciler"
    assert args.log_level == "DEBUG"


def test_run_reconciler_builds_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_reconciler` opens a pool, constructs a Reconciler, runs, closes."""
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

    expected_reaper = object()
    expected_resetter = object()

    class _FakeProviderComposition:
        def build_provider_resolver(self) -> _FakeResolver:
            return _FakeResolver()

        def build_reconciler_reaper(self) -> object:
            return expected_reaper

        def build_reconciler_transport_resetter(self) -> object:
            return expected_resetter

    monkeypatch.setattr(composition, "ProviderComposition", _FakeProviderComposition)

    constructed: dict[str, object] = {}

    def _fake_init(self: object, pool: object, reaper: object, **kw: object) -> None:
        constructed["reaper"] = reaper
        constructed["resetter"] = kw.get("resetter")

    async def _fake_run(self: object, stop: object) -> None:
        events.append("run")

    monkeypatch.setattr(loop.Reconciler, "__init__", _fake_init)
    monkeypatch.setattr(loop.Reconciler, "run", _fake_run)

    asyncio.run(__main__._run_reconciler(SecretRegistry()))

    assert events == ["open", "discover", "run", "close"]
    assert constructed["reaper"] is expected_reaper
    assert constructed["resetter"] is expected_resetter
