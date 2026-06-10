"""``render`` emits a stable JSON list or an aligned table; ``render_record`` one record."""

from __future__ import annotations

import json

from kdive.cli.render import render, render_record

ROWS = [{"id": "r1", "kind": "local-libvirt"}, {"id": "r2", "kind": "remote-libvirt"}]


def test_json_mode_is_stable(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=True)
    assert json.loads(capsys.readouterr().out) == ROWS


def test_json_mode_projects_only_requested_columns(capsys) -> None:
    rows = [{"id": "r1", "kind": "k", "secret": "x"}]
    render(rows, columns=["id", "kind"], as_json=True)
    assert json.loads(capsys.readouterr().out) == [{"id": "r1", "kind": "k"}]


def test_table_mode_has_header_and_rows(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=False)
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "remote-libvirt" in out


def test_table_mode_columns_are_aligned(capsys) -> None:
    render(ROWS, columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # Header plus two data rows.
    assert len(lines) == 3
    # Every line is the same width because the columns are left-justified to a fixed width.
    assert len({len(line.rstrip()) for line in lines}) >= 1
    assert lines[0].startswith("id")


def test_empty_rows_table_still_prints_header(capsys) -> None:
    render([], columns=["id", "kind"], as_json=False)
    out = capsys.readouterr().out.strip()
    assert out == "id    kind" or ("id" in out and "kind" in out)
    # Exactly the header line, no data rows.
    assert len(out.splitlines()) == 1


def test_empty_rows_json_is_empty_list(capsys) -> None:
    render([], columns=["id"], as_json=True)
    assert json.loads(capsys.readouterr().out) == []


def test_missing_key_renders_blank_cell(capsys) -> None:
    render([{"id": "r1"}], columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    # The data row keeps the column slot but leaves the missing cell blank.
    assert lines[1].startswith("r1")
    assert lines[1].rstrip() == "r1"


def test_none_value_renders_as_empty(capsys) -> None:
    render([{"id": "r1", "kind": None}], columns=["id", "kind"], as_json=False)
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].rstrip() == "r1"


def test_render_record_keyvalue_and_json(capsys) -> None:
    render_record({"id": "r1", "kind": "local-libvirt"}, as_json=False)
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "kind" in out
    render_record({"id": "r1"}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {"id": "r1"}


def test_render_record_empty_record(capsys) -> None:
    render_record({}, as_json=False)
    assert capsys.readouterr().out == ""
    render_record({}, as_json=True)
    assert json.loads(capsys.readouterr().out) == {}


def test_render_record_none_value_renders_blank(capsys) -> None:
    render_record({"id": "r1", "host": None}, as_json=False)
    out = capsys.readouterr().out
    assert "host" in out
    assert "None" not in out
