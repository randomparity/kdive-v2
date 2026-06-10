"""Break-glass mutating verbs each run the token preflight, then call their break-glass tool.

The verbs are driven through fakes for the MCP client so the tests are hermetic: a fake
client returns a ``ToolResponse``-shaped payload and records ``(tool, arguments)``. Each
test asserts the verb maps to the correct ``destructive()``-annotated server tool with the
expected payload, and that the fail-closed token preflight runs first (ADR-0089).
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from kdive.cli.commands import REGISTRY, mutations


class _FakeResult:
    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        self.calls.append((name, arguments))
        return _FakeResult(self._payload)


class _FakeSession:
    def __init__(self, client: _FakeClient, token: str = "x.y.z") -> None:
        self._client = client
        self.token = token

    def client(self) -> _FakeClient:
        return self._client


def _install_session(monkeypatch: pytest.MonkeyPatch, payload: dict | None = None) -> _FakeClient:
    client = _FakeClient(payload or {"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))
    monkeypatch.setattr(mutations, "ensure_token_valid", lambda *a, **k: None)
    return client


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=False, **kwargs)


def test_force_release_calls_breakglass_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch)
    code = asyncio.run(
        mutations.allocations_force_release(_args(allocation_id="al-1", reason="stuck"))
    )
    assert code == 0
    assert client.calls == [("ops.force_release", {"allocation_id": "al-1", "reason": "stuck"})]


def test_force_release_denied_envelope_maps_to_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        payload={
            "object_id": "al-1",
            "status": "error",
            "error_category": "authorization_denied",
            "data": {},
        },
    )
    code = asyncio.run(
        mutations.allocations_force_release(_args(allocation_id="al-1", reason="stuck"))
    )
    assert code == 3


def test_force_teardown_calls_breakglass_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch)
    asyncio.run(mutations.teardown(_args(system_id="sys-1", reason="wedged", force=True)))
    assert client.calls == [("ops.force_teardown", {"system_id": "sys-1", "reason": "wedged"})]


def test_cordon_calls_resources_cordon(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch)
    asyncio.run(mutations.resources_cordon(_args(resource_id="host-1")))
    assert client.calls == [("resources.cordon", {"resource_id": "host-1"})]


def test_drain_calls_resources_drain(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch)
    asyncio.run(
        mutations.resources_drain(_args(resource_id="host-1", mode="force_release", reason="evict"))
    )
    assert client.calls == [
        ("resources.drain", {"resource_id": "host-1", "mode": "force_release", "reason": "evict"})
    ]


def test_drain_defaults_to_passive_mode(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch)
    asyncio.run(mutations.resources_drain(_args(resource_id="host-1", mode=None, reason=None)))
    assert client.calls == [("resources.drain", {"resource_id": "host-1", "mode": "passive"})]


def test_preflight_runs_before_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))

    class _Boom(RuntimeError):
        pass

    def _refuse(*_a: object, **_k: object) -> None:
        raise _Boom

    monkeypatch.setattr(mutations, "ensure_token_valid", _refuse)
    with pytest.raises(_Boom):
        asyncio.run(mutations.allocations_force_release(_args(allocation_id="al-1", reason="r")))
    assert client.calls == []


def test_teardown_requires_force_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install_session(monkeypatch)
    with pytest.raises(SystemExit):
        asyncio.run(mutations.teardown(_args(system_id="sys-1", reason="r", force=False)))
    assert client.calls == []


def test_preflight_reads_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"object_id": "o", "status": "ok", "data": {}})
    seen: list[str] = []

    def _capture(token: str, **_k: object) -> None:
        seen.append(token)

    monkeypatch.setattr(
        mutations, "_session_factory", lambda: _FakeSession(client, token="tok-123")
    )
    monkeypatch.setattr(mutations, "ensure_token_valid", _capture)
    asyncio.run(mutations.resources_cordon(_args(resource_id="host-1")))
    assert seen == ["tok-123"]


def test_mutating_verbs_are_registered_and_not_read_only() -> None:
    mutating_tools = {
        "ops.force_release",
        "ops.force_teardown",
        "resources.cordon",
        "resources.drain",
    }
    registered = {verb.tool for verb in REGISTRY if not verb.read_only}
    assert mutating_tools <= registered
    for verb in REGISTRY:
        if verb.tool in mutating_tools:
            assert verb.read_only is False
