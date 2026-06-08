from __future__ import annotations

import pytest

from kdive.security.artifacts.artifact_search import (
    ArtifactSearchInputError,
    parse_literal_terms,
    search_text,
)


def test_parse_literal_terms_splits_or_terms() -> None:
    assert parse_literal_terms("__d_lookup|Oops") == ("__d_lookup", "Oops")


@pytest.mark.parametrize("pattern", ["", "a||b", "bad\x00term"])
def test_parse_literal_terms_rejects_bad_patterns(pattern: str) -> None:
    with pytest.raises(ArtifactSearchInputError):
        parse_literal_terms(pattern)


def test_parse_literal_terms_rejects_too_many_terms() -> None:
    with pytest.raises(ArtifactSearchInputError):
        parse_literal_terms("|".join(f"t{i}" for i in range(17)))


def test_search_text_returns_bounded_context() -> None:
    data = b"line one\npanic start\nRIP: __d_lookup+0x1\nnext line\n"
    result = search_text(
        data,
        pattern="__d_lookup|Oops",
        before_lines=1,
        after_lines=1,
        max_matches=5,
    )
    assert result.match_count == 1
    assert result.truncated is False
    assert result.matches[0]["line"] == 3
    assert result.matches[0]["before"] == ["panic start"]
    assert result.matches[0]["after"] == ["next line"]


def test_search_text_clips_long_lines_and_total_json() -> None:
    data = ("x" * 900 + " NEEDLE\n").encode()
    result = search_text(data, pattern="NEEDLE", before_lines=0, after_lines=0, max_matches=1)
    assert len(result.matches[0]["text"]) <= 512 + len("...[clipped]")
