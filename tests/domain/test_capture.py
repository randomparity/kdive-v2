"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod


def test_vocabulary_has_four_methods() -> None:
    assert {m.value for m in CaptureMethod} == {"console", "host_dump", "gdbstub", "kdump"}
