from __future__ import annotations

from scripts.coverage_campaign.gridgen import generate_rows


def test_generate_rows_covers_known_tools_with_correct_metadata() -> None:
    rows = {r.tool: r for r in generate_rows()}
    assert rows["resources.list"].annotation == "read_only"
    assert rows["resources.list"].plane == "resources"
    assert rows["runs.build"].maturity == "partial"
    assert rows["runs.build"].annotation == "mutating"
    assert rows["control.force_crash"].annotation == "destructive"
    assert rows["control.force_crash"].destructive_member is True
    assert all(r.plane for r in rows.values())
    assert all(r.maturity in {"implemented", "partial", "planned"} for r in rows.values())


def test_generate_rows_is_nonempty_and_unique() -> None:
    rows = generate_rows()
    names = [r.tool for r in rows]
    assert len(names) > 50
    assert len(names) == len(set(names))
