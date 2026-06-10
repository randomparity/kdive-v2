"""The read-only, fail-closed passthrough gate refuses anything not provably read-only."""

from __future__ import annotations

import pytest

from kdive.cli.passthrough import NotReadOnlyError, assert_read_only


class _Tool:
    def __init__(self, read_only: object) -> None:
        self.annotations = type("A", (), {"readOnlyHint": read_only})()


def test_read_only_tool_allowed() -> None:
    assert_read_only("resources.list", _Tool(True))


@pytest.mark.parametrize("hint", [False, None])
def test_non_read_only_refused(hint: object) -> None:
    with pytest.raises(NotReadOnlyError):
        assert_read_only("ops.force_release", _Tool(hint))


def test_truthy_non_true_hint_refused() -> None:
    # Only a literal ``True`` passes; a truthy-but-not-True value fails closed.
    with pytest.raises(NotReadOnlyError):
        assert_read_only("ambiguous.tool", _Tool(1))


def test_missing_annotations_refused() -> None:
    with pytest.raises(NotReadOnlyError):
        assert_read_only("mystery.tool", object())


def test_none_tool_refused() -> None:
    with pytest.raises(NotReadOnlyError):
        assert_read_only("absent.tool", None)


def test_refusal_message_names_the_tool() -> None:
    with pytest.raises(NotReadOnlyError, match="ops.teardown"):
        assert_read_only("ops.teardown", _Tool(False))
