"""_docmeta: annotation constructors + the reviewed destructive set."""

from __future__ import annotations

from kdive.mcp.tools import _docmeta


def test_read_only_sets_only_read_hint() -> None:
    a = _docmeta.read_only()
    assert a.readOnlyHint is True
    assert a.destructiveHint is not True


def test_destructive_sets_destructive_not_readonly() -> None:
    a = _docmeta.destructive()
    assert a.destructiveHint is True
    assert a.readOnlyHint is not True


def test_mutating_is_not_readonly_not_destructive() -> None:
    a = _docmeta.mutating()
    assert a.readOnlyHint is not True
    assert a.destructiveHint is not True


def test_destructive_tools_set_is_the_reviewed_set() -> None:
    assert (
        frozenset(
            {
                "control.power",
                "control.force_crash",
                "systems.teardown",
                "systems.reprovision",
                "ops.force_teardown",
                "ops.force_release",
            }
        )
        == _docmeta.DESTRUCTIVE_TOOLS
    )
