"""gdb/MI record parsing helpers for the local-libvirt debug provider."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from pygdbmi.gdbmiparser import parse_response

# The literal MI prompt terminator gdb emits between command results; not a record.
_MI_PROMPT = "(gdb)"
# Keys pygdbmi may emit on a parsed record; whitelist so an unexpected extra key is dropped
# rather than tripping the extra="forbid" model boundary.
_KNOWN_KEYS = ("type", "message", "payload", "token", "stream")


class _MiModel(BaseModel):
    """Frozen wire shape for parsed gdb/MI records (``extra="forbid"``)."""

    model_config = ConfigDict(extra="forbid")


class MiRecord(_MiModel):
    """One parsed gdb/MI record (gdb manual "GDB/MI Output Syntax")."""

    type: str
    message: str | None = None
    payload: dict[str, Any] | list[Any] | str | None = None
    token: int | None = None
    stream: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MiRecord:
        return cls(**{key: raw[key] for key in _KNOWN_KEYS if key in raw})

    @staticmethod
    def first_result(records: list[MiRecord]) -> MiRecord | None:
        """The first result-class record, or None."""
        return next((record for record in records if record.type == "result"), None)


def mi_int(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.lstrip("-").isdigit() else None


def payload_dict(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _payload_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_rows(value: object) -> list[dict[str, Any]]:
    return [row for row in _payload_list(value) if isinstance(row, dict)]


def result_payload_dict(records: list[MiRecord]) -> dict[str, Any]:
    result = MiRecord.first_result(records)
    if result is None:
        return {}
    return payload_dict(result.payload)


def breakpoint_rows(records: list[MiRecord]) -> list[dict[str, Any]]:
    payload = result_payload_dict(records)
    table = payload_dict(payload.get("BreakpointTable"))
    rows: list[dict[str, Any]] = []
    for row in _dict_rows(table.get("body")):
        entry = row.get("bkpt")
        if isinstance(entry, dict):
            rows.append(entry)
    return rows


def register_names(records: list[MiRecord]) -> list[str]:
    names = result_payload_dict(records).get("register-names")
    return [name for name in _payload_list(names) if isinstance(name, str)]


def register_values_by_number(records: list[MiRecord]) -> dict[str, object]:
    rows = _dict_rows(result_payload_dict(records).get("register-values"))
    by_number: dict[str, object] = {}
    for row in rows:
        number = row.get("number")
        if isinstance(number, str):
            by_number[number] = row.get("value")
    return by_number


def memory_segments(records: list[MiRecord]) -> list[dict[str, Any]]:
    return _dict_rows(result_payload_dict(records).get("memory"))


def parse_mi_records(text: str) -> list[MiRecord]:
    """Parse newline-delimited MI output into typed records."""
    records: list[MiRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == _MI_PROMPT:
            continue
        records.append(MiRecord.from_raw(parse_response(stripped)))
    return records
