"""``tool call`` tiered passthrough: opt-in flags, preflight, destructive confirm, envelope exit.

The flow is driven through fakes for the MCP client (the ``dispatch._session_factory`` seam is
monkeypatched, mirroring ``tests/cli/test_mutation_verbs.py``) so the tests are hermetic. They
assert the tier opt-in, the token-``exp`` preflight on both mutating tiers, the confirmation,
and the envelope-derived exit code (ADR-0105).
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from kdive.cli import dispatch
from kdive.cli.__main__ import build_parser


class _Annotations:
    def __init__(self, **hints: object) -> None:
        for key, value in hints.items():
            setattr(self, key, value)


class _Tool:
    def __init__(self, name: str, **hints: object) -> None:
        self.name = name
        self.annotations = _Annotations(**hints)


def _read_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=True)


def _mutating_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=False)


def _destructive_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=False, destructiveHint=True)


class _FakeResult:
    def __init__(self, envelope: dict) -> None:
        self.structured_content = envelope
        self.data = envelope


class _FakeClient:
    def __init__(self, tools: list[_Tool], envelope: dict) -> None:
        self._tools = tools
        self._envelope = envelope
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[_Tool]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        self.calls.append((name, arguments))
        return _FakeResult(self._envelope)


class _FakeSession:
    def __init__(self, client: _FakeClient, token: str = "x.y.z") -> None:
        self._client = client
        self.token = token

    def client(self) -> _FakeClient:
        return self._client


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tool: _Tool,
    *,
    envelope: dict | None = None,
    token: str = "x.y.z",
) -> _FakeClient:
    client = _FakeClient([tool], envelope or {"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(dispatch, "_session_factory", lambda: _FakeSession(client, token=token))
    monkeypatch.setattr(dispatch, "ensure_token_valid", lambda *a, **k: None)
    monkeypatch.setattr(dispatch.sys.stdin, "isatty", lambda: False)
    return client


def _args(name: str, **kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "payload": "{}",
        "allow_mutating": False,
        "allow_destructive": False,
        "yes": False,
    }
    base.update(kwargs)
    return argparse.Namespace(name=name, **base)


def _run(args: argparse.Namespace) -> int:
    return asyncio.run(dispatch._tool_call(args))


# --- _confirm_destructive --------------------------------------------------------------------


def test_confirm_assume_yes_does_not_read() -> None:
    def _boom() -> str:
        raise AssertionError("read_line must not be called when assume_yes is set")

    assert dispatch._confirm_destructive("t", assume_yes=True, is_tty=True, read_line=_boom) is True


def test_confirm_tty_yes() -> None:
    assert (
        dispatch._confirm_destructive("t", assume_yes=False, is_tty=True, read_line=lambda: "yes")
        is True
    )


@pytest.mark.parametrize("answer", ["no", "", "  ", "y"])
def test_confirm_tty_non_yes(answer: str) -> None:
    assert (
        dispatch._confirm_destructive("t", assume_yes=False, is_tty=True, read_line=lambda: answer)
        is False
    )


def test_confirm_tty_eof_is_false() -> None:
    def _eof() -> str:
        raise EOFError

    decision = dispatch._confirm_destructive("t", assume_yes=False, is_tty=True, read_line=_eof)
    assert decision is False


def test_confirm_non_tty_without_yes_does_not_read() -> None:
    def _boom() -> str:
        raise AssertionError("read_line must not be called on a non-TTY")

    assert (
        dispatch._confirm_destructive("t", assume_yes=False, is_tty=False, read_line=_boom) is False
    )


# --- _tool_call: tier admission --------------------------------------------------------------


def test_mutating_tool_refused_without_flag(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(monkeypatch, _mutating_tool("ops.cordon"))
    assert _run(_args("ops.cordon")) == 3
    assert client.calls == []


def test_mutating_tool_admitted_with_flag(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(monkeypatch, _mutating_tool("ops.cordon"))
    assert _run(_args("ops.cordon", allow_mutating=True)) == 0
    assert client.calls == [("ops.cordon", {})]


def test_destructive_tool_refused_with_only_mutating_flag(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, _destructive_tool("ops.force_teardown"))
    assert _run(_args("ops.force_teardown", allow_mutating=True)) == 3
    assert client.calls == []


def test_unknown_tool_refused_even_with_destructive_flag(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A tool not in the server list classifies UNKNOWN and is unreachable at any tier.
    client = _install(monkeypatch, _read_tool("present.tool"))
    assert _run(_args("absent.tool", allow_destructive=True, yes=True)) == 3
    assert client.calls == []


def test_read_only_tool_dispatched_with_no_flag(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(monkeypatch, _read_tool("resources.list"))
    assert _run(_args("resources.list")) == 0
    assert client.calls == [("resources.list", {})]


# --- _tool_call: token preflight -------------------------------------------------------------


def test_preflight_refuses_expired_token_on_mutating(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, _mutating_tool("ops.cordon"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_args("ops.cordon", allow_mutating=True)) == 3
    assert client.calls == []


def test_preflight_refuses_expired_token_on_destructive(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, _destructive_tool("ops.force_teardown"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_args("ops.force_teardown", allow_destructive=True, yes=True)) == 3
    assert client.calls == []


def test_read_only_not_subject_to_preflight(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(monkeypatch, _read_tool("resources.list"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_args("resources.list")) == 0
    assert client.calls == [("resources.list", {})]


def _raise_expiring(*_a: object, **_k: object) -> None:
    from kdive.cli.commands.mutations import TokenExpiringError

    raise TokenExpiringError("expired")


# --- _tool_call: destructive confirmation ----------------------------------------------------


def test_destructive_non_tty_without_yes_refused_naming_yes(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, _destructive_tool("ops.force_teardown"))
    assert _run(_args("ops.force_teardown", allow_destructive=True)) == 3
    assert client.calls == []
    assert "--yes" in capsys.readouterr().out


def test_destructive_with_yes_dispatched(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(monkeypatch, _destructive_tool("ops.force_teardown"))
    assert _run(_args("ops.force_teardown", allow_destructive=True, yes=True)) == 0
    assert client.calls == [("ops.force_teardown", {})]


# --- _tool_call: envelope-derived exit code --------------------------------------------------


def test_admitted_call_denied_envelope_exits_3(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    denied = {
        "object_id": "host-1",
        "status": "error",
        "error_category": "authorization_denied",
        "data": {},
    }
    client = _install(monkeypatch, _mutating_tool("ops.cordon"), envelope=denied)
    assert _run(_args("ops.cordon", allow_mutating=True)) == 3
    # The call WAS dispatched; the denial is a returned envelope, not a refusal.
    assert client.calls == [("ops.cordon", {})]


def test_admitted_call_clean_envelope_exits_0(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install(monkeypatch, _mutating_tool("ops.cordon"))
    assert _run(_args("ops.cordon", allow_mutating=True)) == 0


# --- parser flags ----------------------------------------------------------------------------


def test_tool_call_defaults_to_no_opt_in() -> None:
    args = build_parser().parse_args(["tool", "call", "x"])
    assert args.allow_mutating is False
    assert args.allow_destructive is False
    assert args.yes is False


def test_tool_call_opt_in_flags_set_their_dest() -> None:
    args = build_parser().parse_args(
        ["tool", "call", "x", "--allow-mutating", "--allow-destructive", "--yes"]
    )
    assert args.allow_mutating is True
    assert args.allow_destructive is True
    assert args.yes is True


def test_tool_call_json_payload_unaffected_by_new_flags() -> None:
    argv = ["tool", "call", "x", "--json", '{"a": 1}', "--allow-mutating"]
    args = build_parser().parse_args(argv)
    assert args.payload == '{"a": 1}'
    assert args.allow_mutating is True
