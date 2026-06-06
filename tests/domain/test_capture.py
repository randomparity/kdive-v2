"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

from kdive.domain.capture import LOCAL_LIBVIRT_SUPPORTED, CaptureMethod


def test_vocabulary_has_four_methods() -> None:
    assert {m.value for m in CaptureMethod} == {"console", "host_dump", "gdbstub", "kdump"}


def test_local_libvirt_supports_three_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert (
        frozenset({CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB})
        == LOCAL_LIBVIRT_SUPPORTED
    )
    assert CaptureMethod.KDUMP not in LOCAL_LIBVIRT_SUPPORTED
