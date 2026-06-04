"""Tests for the local-libvirt Retrieve plane (ADR-0031)."""

from __future__ import annotations

import pytest

from kdive.providers.local_libvirt.retrieve import crash_command_rejection_reason

_ALLOW = frozenset({"bt", "log", "ps", "p", "rd"})


@pytest.mark.parametrize("command", ["bt", "  log ", "ps -A", "p jiffies"])
def test_allowed_commands_pass(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is None


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "bt | sh",
        "log > /etc/passwd",
        "rd `whoami`",
        "ps; reboot",
        "log $(id)",
        "!touch x",
        "log\nbt",
        "nuke now",
    ],
)
def test_rejected_commands_have_a_reason(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is not None
