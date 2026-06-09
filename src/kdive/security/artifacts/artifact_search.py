"""Bounded literal text search over redacted artifacts (ADR-0064)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypedDict

MAX_PATTERN_CHARS = 256
MAX_TERMS = 16
MAX_LINE_CHARS = 512
MAX_MATCHES_JSON_CHARS = 64 * 1024
CLIPPED = "...[clipped]"


class ArtifactSearchInputError(ValueError):
    """The requested search is malformed or outside the ADR-0064 bounds."""


class SearchMatch(TypedDict):
    """One bounded literal match window."""

    line: int
    text: str
    before: list[str]
    after: list[str]


@dataclass(frozen=True)
class SearchResult:
    """A bounded search result suitable for ``ToolResponse.data``."""

    matches: list[SearchMatch]
    match_count: int
    truncated: bool

    def matches_json(self) -> str:
        return json.dumps(self.matches, ensure_ascii=False, separators=(",", ":"))


def parse_literal_terms(pattern: str) -> tuple[str, ...]:
    """Parse a grep-style literal OR pattern into bounded terms."""
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
        raise ArtifactSearchInputError("pattern must be 1-256 characters")
    if "\x00" in pattern:
        raise ArtifactSearchInputError("pattern must not contain NUL")
    terms = tuple(part for part in pattern.split("|"))
    if not terms or any(term == "" for term in terms):
        raise ArtifactSearchInputError("pattern contains an empty term")
    if len(terms) > MAX_TERMS:
        raise ArtifactSearchInputError("pattern has too many terms")
    return terms


def _clip(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + CLIPPED


def _bounded_int(value: int, *, low: int, high: int, label: str) -> int:
    if value < low or value > high:
        raise ArtifactSearchInputError(f"{label} out of range")
    return value


def search_text(
    data: bytes,
    *,
    pattern: str,
    before_lines: int = 2,
    after_lines: int = 4,
    max_matches: int = 20,
) -> SearchResult:
    """Search UTF-8-ish bytes line-by-line with bounded context windows."""
    terms = parse_literal_terms(pattern)
    before_lines = _bounded_int(before_lines, low=0, high=10, label="before_lines")
    after_lines = _bounded_int(after_lines, low=0, high=20, label="after_lines")
    max_matches = _bounded_int(max_matches, low=1, high=50, label="max_matches")
    lines = data.decode("utf-8", errors="replace").splitlines()
    matches: list[SearchMatch] = []
    truncated = False
    for idx, line in enumerate(lines):
        if not any(term in line for term in terms):
            continue
        start = max(0, idx - before_lines)
        end = min(len(lines), idx + after_lines + 1)
        candidate: SearchMatch = {
            "line": idx + 1,
            "text": _clip(line),
            "before": [_clip(item) for item in lines[start:idx]],
            "after": [_clip(item) for item in lines[idx + 1 : end]],
        }
        trial = [*matches, candidate]
        encoded = json.dumps(trial, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > MAX_MATCHES_JSON_CHARS:
            truncated = True
            break
        matches.append(candidate)
        if len(matches) >= max_matches:
            truncated = True
            break
    return SearchResult(matches=matches, match_count=len(matches), truncated=truncated)
