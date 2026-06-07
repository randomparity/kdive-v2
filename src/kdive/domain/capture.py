"""The provider-agnostic crash-capture method vocabulary (ADR-0049 Decision 1)."""

from __future__ import annotations

from enum import StrEnum


class CaptureMethod(StrEnum):
    """A capture verb; each provider maps it to a mechanism (or rejects it)."""

    CONSOLE = "console"
    HOST_DUMP = "host_dump"
    GDBSTUB = "gdbstub"
    KDUMP = "kdump"
