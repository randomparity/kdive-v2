"""The tiered passthrough gate classifies tools and admits only tiers <= ``max_tier``.

``classify_tool`` derives a :class:`ToolTier` from the server's ``ToolAnnotations`` and
``assert_tool_allowed`` admits/refuses against a caller-authorized ``max_tier``. ``UNKNOWN`` is
fail-closed and unreachable at every tier (ADR-0105).
"""

from __future__ import annotations

import pytest

from kdive.cli.passthrough import (
    ToolNotAllowedError,
    ToolTier,
    assert_tool_allowed,
    classify_tool,
)


class _Annotations:
    def __init__(self, **hints: object) -> None:
        for key, value in hints.items():
            setattr(self, key, value)


class _Tool:
    def __init__(self, annotations: object) -> None:
        self.annotations = annotations


def _tool(**hints: object) -> _Tool:
    return _Tool(_Annotations(**hints))


# --- classify_tool ---------------------------------------------------------------------------


def test_read_only_hint_classifies_read_only() -> None:
    assert classify_tool(_tool(readOnlyHint=True)) is ToolTier.READ_ONLY


def test_read_only_dominates_a_co_set_destructive_hint() -> None:
    # A tool explicitly marked read-only stays READ_ONLY even if destructiveHint is also set.
    assert classify_tool(_tool(readOnlyHint=True, destructiveHint=True)) is ToolTier.READ_ONLY


def test_not_read_only_with_destructive_hint_classifies_destructive() -> None:
    assert classify_tool(_tool(readOnlyHint=False, destructiveHint=True)) is ToolTier.DESTRUCTIVE


@pytest.mark.parametrize("destructive", [False, None])
def test_not_read_only_without_destructive_classifies_mutating(destructive: object) -> None:
    tier = classify_tool(_tool(readOnlyHint=False, destructiveHint=destructive))
    assert tier is ToolTier.MUTATING


def test_not_read_only_missing_destructive_classifies_mutating() -> None:
    assert classify_tool(_tool(readOnlyHint=False)) is ToolTier.MUTATING


def test_read_only_hint_none_classifies_unknown() -> None:
    assert classify_tool(_tool(readOnlyHint=None)) is ToolTier.UNKNOWN


def test_truthy_non_true_read_only_hint_classifies_unknown() -> None:
    assert classify_tool(_tool(readOnlyHint=1)) is ToolTier.UNKNOWN


def test_missing_annotations_classifies_unknown() -> None:
    assert classify_tool(object()) is ToolTier.UNKNOWN


def test_none_tool_classifies_unknown() -> None:
    assert classify_tool(None) is ToolTier.UNKNOWN


# --- assert_tool_allowed ---------------------------------------------------------------------


def test_read_only_admitted_at_every_max_tier() -> None:
    for max_tier in (ToolTier.READ_ONLY, ToolTier.MUTATING, ToolTier.DESTRUCTIVE):
        assert (
            assert_tool_allowed("resources.list", _tool(readOnlyHint=True), max_tier=max_tier)
            is ToolTier.READ_ONLY
        )


def test_mutating_admitted_at_mutating_and_destructive() -> None:
    tool = _tool(readOnlyHint=False)
    for max_tier in (ToolTier.MUTATING, ToolTier.DESTRUCTIVE):
        assert assert_tool_allowed("ops.cordon", tool, max_tier=max_tier) is ToolTier.MUTATING


def test_destructive_admitted_only_at_destructive() -> None:
    tool = _tool(readOnlyHint=False, destructiveHint=True)
    assert (
        assert_tool_allowed("ops.force_teardown", tool, max_tier=ToolTier.DESTRUCTIVE)
        is ToolTier.DESTRUCTIVE
    )


def test_mutating_refused_at_read_only_names_allow_mutating() -> None:
    with pytest.raises(ToolNotAllowedError, match="--allow-mutating") as exc:
        assert_tool_allowed("ops.cordon", _tool(readOnlyHint=False), max_tier=ToolTier.READ_ONLY)
    assert "ops.cordon" in str(exc.value)


def test_destructive_refused_at_mutating_names_allow_destructive() -> None:
    tool = _tool(readOnlyHint=False, destructiveHint=True)
    with pytest.raises(ToolNotAllowedError, match="--allow-destructive") as exc:
        assert_tool_allowed("ops.force_teardown", tool, max_tier=ToolTier.MUTATING)
    assert "ops.force_teardown" in str(exc.value)


@pytest.mark.parametrize("max_tier", [ToolTier.READ_ONLY, ToolTier.MUTATING, ToolTier.DESTRUCTIVE])
def test_unknown_refused_at_every_tier_naming_no_flag(max_tier: ToolTier) -> None:
    with pytest.raises(ToolNotAllowedError) as exc:
        assert_tool_allowed("mystery.tool", object(), max_tier=max_tier)
    message = str(exc.value)
    assert "mystery.tool" in message
    assert "--allow-mutating" not in message and "--allow-destructive" not in message


def test_none_tool_refused_even_at_destructive() -> None:
    with pytest.raises(ToolNotAllowedError):
        assert_tool_allowed("absent.tool", None, max_tier=ToolTier.DESTRUCTIVE)
