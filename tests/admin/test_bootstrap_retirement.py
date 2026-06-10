"""The hand-rolled app bootstrap is retired (ADR-0088 decision 9).

The `stack` supervisor and the `install-compose`/`print-local-env` dev crutches are
removed; only `migrate`/`install-fixtures`/`seed-demo` remain. The image (or the compose
app tier) is the bring-up path that replaces them.
"""

from __future__ import annotations

import pytest

from kdive.__main__ import build_parser


def test_removed_subcommands_exit_on_parse() -> None:
    parser = build_parser()
    for removed in ("stack", "install-compose", "print-local-env"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])


def test_retained_subcommands_still_parse() -> None:
    parser = build_parser()
    for retained in ("server", "worker", "reconciler", "migrate", "seed-demo"):
        args = parser.parse_args([retained])
        assert args.command == retained
    assert parser.parse_args(["install-fixtures"]).command == "install-fixtures"


def test_run_stack_not_importable() -> None:
    import kdive.admin.bootstrap as bootstrap

    for removed in ("run_stack", "install_compose", "print_local_env", "supervisor_commands"):
        assert not hasattr(bootstrap, removed)
