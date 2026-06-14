"""Coverage-campaign HTTP driver tests."""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from scripts.coverage_campaign import drive


class _Envelope:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return self._payload


class _Client:
    calls: list[tuple[str, str, str, dict[str, object]]] = []

    def __init__(self, base: str, token: str) -> None:
        self._base = base
        self._token = token

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, tool: str, **args: object) -> _Envelope:
        self.calls.append((self._base, self._token, tool, args))
        return _Envelope({"tool": tool, "args": args})


class _ClientFactory:
    @staticmethod
    def over_http(base: str, token: str) -> _Client:
        return _Client(base, token)


def _namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "base": "http://server/mcp",
        "project": "demo",
        "subject": "agent",
        "role": "operator",
        "platform_roles": "platform_admin,",
        "tool": "resources.list",
        "args": '{"limit": 1}',
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_run_mints_token_calls_tool_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _Client.calls = []
    monkeypatch.setattr(drive, "oidc_issuer_from_env", lambda: "issuer")
    monkeypatch.setattr(
        drive,
        "mint_token",
        lambda issuer, **claims: f"{issuer}:{claims['subject']}:{claims['platform_roles'][0]}",
    )
    monkeypatch.setattr(drive, "LiveStackClient", _ClientFactory)

    rc = asyncio.run(drive._run(_namespace()))

    assert rc == 0
    assert _Client.calls == [
        ("http://server/mcp", "issuer:agent:platform_admin", "resources.list", {"limit": 1})
    ]
    assert json.loads(capsys.readouterr().out) == [{"tool": "resources.list", "args": {"limit": 1}}]


def test_run_reports_transport_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingFactory:
        @staticmethod
        def over_http(base: str, token: str) -> object:
            raise RuntimeError(f"cannot reach {base} with {token}")

    monkeypatch.setattr(drive, "oidc_issuer_from_env", lambda: "issuer")
    monkeypatch.setattr(drive, "mint_token", lambda issuer, **_claims: f"{issuer}:token")
    monkeypatch.setattr(drive, "LiveStackClient", FailingFactory)

    rc = asyncio.run(drive._run(_namespace(role=None, platform_roles="")))

    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {
        "driver_error": "RuntimeError",
        "message": "cannot reach http://server/mcp with issuer:token",
    }
