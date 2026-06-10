"""CLI argument parsing for `python -m kdive`."""

from __future__ import annotations

import pytest

from kdive.__main__ import build_parser


def test_server_subcommand_parses() -> None:
    args = build_parser().parse_args(["server"])
    assert args.command == "server"
    # No flag → None; the INFO default is supplied by the config registry, not argparse.
    assert args.log_level is None


def test_worker_subcommand_parses_with_log_level() -> None:
    args = build_parser().parse_args(["--log-level", "DEBUG", "worker"])
    assert args.command == "worker"
    assert args.log_level == "DEBUG"


def test_no_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
