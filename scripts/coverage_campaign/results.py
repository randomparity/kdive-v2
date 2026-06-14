"""Per-cell verdict record and the grid-markdown renderer (spec section 2)."""

from __future__ import annotations

from dataclasses import dataclass

from scripts.coverage_campaign.gridgen import CensusRow

_PROVIDERS = ("local-libvirt", "remote-libvirt", "fault-inject")
_GLYPH = {"pass": "✅", "gap": "⚠️", "fail": "❌", "blocked": "⏭"}


@dataclass(frozen=True)
class CellResult:
    tool: str
    provider: str
    verdict: str  # "pass" | "gap" | "fail" | "blocked"
    issue: int | None


def _cell(result: CellResult | None) -> str:
    if result is None:
        return "—"
    glyph = _GLYPH[result.verdict]
    return f"{glyph}(#{result.issue})" if result.issue is not None else glyph


def merge_and_render(rows: list[CensusRow], results: list[CellResult]) -> str:
    by_key = {(r.tool, r.provider): r for r in results}
    header = "| Tool | Plane | Maturity | Annotation | " + " | ".join(_PROVIDERS) + " |"
    sep = "|" + "---|" * (4 + len(_PROVIDERS))
    lines = [header, sep]
    for row in rows:
        cells = [_cell(by_key.get((row.tool, p))) for p in _PROVIDERS]
        marker = "★" if row.destructive_member else ""
        lines.append(
            f"| `{row.tool}`{marker} | {row.plane} | {row.maturity} | "
            f"{row.annotation} | " + " | ".join(cells) + " |"
        )
    return "\n".join(lines) + "\n"
