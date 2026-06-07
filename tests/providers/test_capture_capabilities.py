"""Provider-runtime capture capability tests."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.providers.composition import build_default_provider_runtime


def test_local_libvirt_supports_three_methods_now_not_kdump() -> None:
    # kdump joins via #115; it is in the vocabulary but not yet supported.
    assert build_default_provider_runtime().supported_capture_methods() == frozenset(
        {CaptureMethod.CONSOLE, CaptureMethod.HOST_DUMP, CaptureMethod.GDBSTUB}
    )
