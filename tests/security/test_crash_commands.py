"""Direct tests for crash command validation."""

from __future__ import annotations

import pytest

from kdive.security.crash_commands import crash_command_rejection_reason

_ALLOW = frozenset({"bt", "log", "ps", "p", "rd", "kmem", "sys"})


@pytest.mark.parametrize("command", ["", "   "])
def test_empty_commands_are_rejected(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) == "empty command"


@pytest.mark.parametrize("command", ["log\nbt", "log\tbt", "log\x00bt", "log\x7fbt"])
def test_control_characters_are_rejected(command: str) -> None:
    assert (
        crash_command_rejection_reason(command, _ALLOW)
        == "command contains a newline or control character"
    )


def test_shell_escape_is_rejected() -> None:
    assert crash_command_rejection_reason("!echo pwned", _ALLOW) == (
        "shell escape ('!') is not permitted"
    )


@pytest.mark.parametrize(
    ("command", "token"),
    [
        ("bt | sh", "|"),
        ("log > out", ">"),
        ("log < in", "<"),
        ("rd `whoami`", "`"),
        ("log $(id)", "$("),
        ("ps; reboot", ";"),
        ("ps &", "&"),
    ],
)
def test_shell_metacharacters_are_rejected(command: str, token: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) == (
        f"disallowed metacharacter {token!r}"
    )


def test_unknown_verbs_are_rejected() -> None:
    assert crash_command_rejection_reason("reboot now", _ALLOW) == (
        "verb 'reboot' is not in the crash command allowlist"
    )


@pytest.mark.parametrize("command", ["BT", "LoG", "Ps -A", "P jiffies"])
def test_allowed_verbs_are_case_insensitive(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is None


@pytest.mark.parametrize("command", ["bt", "log", "ps -A", "p jiffies", "rd -x ffff", "sys"])
def test_allowed_read_only_commands_pass(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is None
