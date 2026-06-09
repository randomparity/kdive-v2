"""Focused parser-helper tests for local-libvirt gdb/MI records."""

from __future__ import annotations

import pytest

from kdive.providers.local_libvirt.debug.mi_protocol import (
    MiRecord,
    breakpoint_rows,
    memory_segments,
    mi_int,
    parse_mi_records,
    register_names,
    register_values_by_number,
    result_payload_dict,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '^done,bkpt={number="1",type="hw breakpoint",addr="0x1",func="panic"}',
            MiRecord(
                type="result",
                message="done",
                payload={
                    "bkpt": {
                        "number": "1",
                        "type": "hw breakpoint",
                        "addr": "0x1",
                        "func": "panic",
                    }
                },
            ),
        ),
        (
            '^done,register-names=["rax","rbx"]',
            MiRecord(type="result", message="done", payload={"register-names": ["rax", "rbx"]}),
        ),
        (
            '^done,memory=[{begin="0x1000",contents="deadbeef"}]',
            MiRecord(
                type="result",
                message="done",
                payload={"memory": [{"begin": "0x1000", "contents": "deadbeef"}]},
            ),
        ),
        ('~"hello\\n"', MiRecord(type="console", payload="hello\n")),
    ],
)
def test_parse_mi_records_accepts_common_wire_shapes(text: str, expected: MiRecord) -> None:
    assert parse_mi_records(f"\n(gdb)\n{text}\n") == [expected]


def test_breakpoint_rows_accepts_table_body_and_ignores_malformed_rows() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
                "BreakpointTable": {
                    "body": [
                        {"bkpt": {"number": "1", "func": "panic"}},
                        {"bkpt": "not-a-dict"},
                        {"other": {"number": "2"}},
                        "not-a-row",
                    ]
                }
            },
        )
    ]

    assert breakpoint_rows(records) == [{"number": "1", "func": "panic"}]


@pytest.mark.parametrize(
    "records",
    [
        [],
        [MiRecord(type="console", payload="text")],
        [MiRecord(type="result", message="done", payload="not-a-dict")],
        [MiRecord(type="result", message="done", payload={"BreakpointTable": "bad"})],
        [MiRecord(type="result", message="done", payload={"BreakpointTable": {"body": "bad"}})],
    ],
)
def test_breakpoint_rows_returns_empty_for_malformed_payloads(
    records: list[MiRecord],
) -> None:
    assert breakpoint_rows(records) == []


def test_register_helpers_filter_unexpected_payload_shapes() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
                "register-names": ["rax", 0, "rbx", None],
                "register-values": [
                    {"number": "0", "value": "0x1"},
                    {"number": 1, "value": "ignored"},
                    {"number": "2"},
                    "not-a-row",
                ],
            },
        )
    ]

    assert register_names(records) == ["rax", "rbx"]
    assert register_values_by_number(records) == {"0": "0x1", "2": None}


@pytest.mark.parametrize(
    "records",
    [
        [],
        [MiRecord(type="result", message="done", payload={"register-names": "rax"})],
        [MiRecord(type="result", message="done", payload={"register-values": {"number": "0"}})],
    ],
)
def test_register_helpers_return_empty_for_malformed_payloads(records: list[MiRecord]) -> None:
    assert register_names(records) == []
    assert register_values_by_number(records) == {}


def test_memory_segments_filters_non_dict_rows() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={"memory": [{"contents": "deadbeef"}, "bad", {"begin": "0x1000"}]},
        )
    ]

    assert memory_segments(records) == [{"contents": "deadbeef"}, {"begin": "0x1000"}]


@pytest.mark.parametrize(
    "records",
    [
        [],
        [MiRecord(type="console", payload="text")],
        [MiRecord(type="result", message="done", payload={"memory": "not-a-list"})],
    ],
)
def test_memory_segments_returns_empty_for_malformed_payloads(records: list[MiRecord]) -> None:
    assert memory_segments(records) == []


def test_result_payload_dict_returns_empty_without_result_dict() -> None:
    assert result_payload_dict([]) == {}
    assert result_payload_dict([MiRecord(type="console", payload="text")]) == {}
    assert result_payload_dict([MiRecord(type="result", message="done", payload="text")]) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("42", 42),
        ("-7", -7),
        ("+7", None),
        ("0x10", None),
        (42, None),
        (None, None),
    ],
)
def test_mi_int_accepts_only_decimal_strings(value: object, expected: int | None) -> None:
    assert mi_int(value) == expected
