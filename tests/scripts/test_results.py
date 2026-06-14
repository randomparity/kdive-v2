from __future__ import annotations

from scripts.coverage_campaign.gridgen import CensusRow
from scripts.coverage_campaign.results import CellResult, merge_and_render


def _row(tool: str) -> CensusRow:
    return CensusRow(
        tool=tool,
        plane=tool.split(".")[0],
        maturity="implemented",
        annotation="read_only",
        destructive_member=False,
    )


def test_render_marks_pass_gap_and_na() -> None:
    rows = [_row("resources.list")]
    results = [
        CellResult(tool="resources.list", provider="local-libvirt", verdict="pass", issue=None),
        CellResult(tool="resources.list", provider="remote-libvirt", verdict="gap", issue=42),
    ]
    md = merge_and_render(rows, results)
    assert "resources.list" in md
    assert "✅" in md
    assert "⚠️(#42)" in md
    assert "—" in md


def test_render_is_deterministic() -> None:
    rows = [_row("a.x"), _row("b.y")]
    assert merge_and_render(rows, []) == merge_and_render(rows, [])
