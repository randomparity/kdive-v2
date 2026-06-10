"""The curated read verbs are wired into the parser and reachable through ``dispatch.run``."""

from __future__ import annotations

import argparse
import asyncio

import pytest

from kdive.cli import dispatch
from kdive.cli.__main__ import build_parser
from kdive.cli.commands import REGISTRY


def test_curated_verb_is_a_known_subcommand() -> None:
    args = build_parser().parse_args(["resources", "list"])
    assert args.command == "resources" and args.subcommand == "list"


def test_record_verb_takes_its_positional() -> None:
    args = build_parser().parse_args(["allocations", "get", "al-1"])
    assert args.allocation_id == "al-1"


def test_list_verb_takes_its_optional_filter() -> None:
    args = build_parser().parse_args(["resources", "list", "--kind", "remote-libvirt"])
    assert args.kind == "remote-libvirt"


def test_optional_filter_defaults_to_none() -> None:
    args = build_parser().parse_args(["systems", "list"])
    assert args.state is None


def test_json_flag_accepted_after_the_verb() -> None:
    args = build_parser().parse_args(["resources", "list", "--json"])
    assert args.json is True


def test_json_flag_accepted_before_the_verb() -> None:
    args = build_parser().parse_args(["--json", "resources", "list"])
    assert args.json is True


def test_json_absent_after_verb_does_not_clobber_top_level() -> None:
    # The post-verb --json default is SUPPRESS, so omitting it leaves the top-level value.
    args = build_parser().parse_args(["--json", "resources", "list"])
    assert args.json is True
    args = build_parser().parse_args(["resources", "list"])
    assert args.json is False


def test_every_registry_verb_parses_through_the_built_parser() -> None:
    parser = build_parser()
    for verb in REGISTRY:
        argv = [verb.group, verb.sub, *(f"{p}-val" for p in verb.positionals)]
        args = parser.parse_args(argv)
        assert args.command == verb.group and args.subcommand == verb.sub


def test_dispatch_routes_curated_verb_to_run_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []

    async def _fake_run_verb(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(dispatch.commands, "run_verb", _fake_run_verb)
    args = build_parser().parse_args(["resources", "list"])
    assert asyncio.run(dispatch.run(args)) == 0
    assert seen and seen[0].command == "resources"


def test_dispatch_unknown_command_exits() -> None:
    args = argparse.Namespace(command="nope", subcommand=None)
    with pytest.raises(SystemExit):
        asyncio.run(dispatch.run(args))
