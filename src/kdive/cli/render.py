"""Operator-facing output: a plain aligned table by default, stable JSON with ``--json``.

The curated read verbs (``kdivectl resources list`` etc.) project each row onto a fixed
column set and hand it here. JSON mode emits the same projected columns so scripts get a
stable contract; table mode left-justifies each column to its widest cell. ``None`` and
missing cells render as the empty string so a column slot is never dropped (ADR-0089).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

_GAP = "  "


def _cell(value: object) -> str:
    """Render one cell: ``None`` becomes the empty string, everything else ``str()``."""
    return "" if value is None else str(value)


def render(rows: Sequence[Mapping[str, object]], *, columns: Sequence[str], as_json: bool) -> None:
    """Render a list of rows as an aligned table, or as a stable JSON list.

    Args:
        rows: The records to display; each is projected onto ``columns``.
        columns: The ordered column keys to show. Keys absent from a row render blank.
        as_json: When ``True``, emit a JSON list of the projected rows instead of a table.
    """
    projected = [{c: row.get(c) for c in columns} for row in rows]
    if as_json:
        print(json.dumps(projected, indent=2, default=str))
        return
    widths = {c: len(c) for c in columns}
    for row in projected:
        for column in columns:
            widths[column] = max(widths[column], len(_cell(row[column])))
    print(_GAP.join(column.ljust(widths[column]) for column in columns))
    for row in projected:
        print(_GAP.join(_cell(row[column]).ljust(widths[column]) for column in columns))


def render_record(record: Mapping[str, object], *, as_json: bool) -> None:
    """Render a single record as aligned key/value lines, or as stable JSON.

    The single-record verbs (``describe``/``get``/``show``) return one record, not a row
    list. ``None`` values render as the empty string, matching :func:`render`.

    Args:
        record: The single record to display.
        as_json: When ``True``, emit the record as a JSON object instead of key/value lines.
    """
    if as_json:
        print(json.dumps(dict(record), indent=2, default=str))
        return
    width = max((len(key) for key in record), default=0)
    for key, value in record.items():
        print(f"{key.ljust(width)}{_GAP}{_cell(value)}".rstrip())
