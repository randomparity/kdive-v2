"""The ``doctor`` verb calls ``ops.diagnostics``, renders the verdict, and maps exit codes.

The verb is driven through a fake MCP client so the tests are hermetic: the fake returns a
collection-envelope payload shaped like ``ops.diagnostics`` (``items`` of per-check
envelopes whose ``data`` carries ``status``/``detail``/``fix``/``provider``), and the verb
renders one row per check and returns the gate-safe exit code (ADR-0091 §5):

- all ``pass`` → ``0``
- any ``fail`` → nonzero (the contract-violation code, dominating a co-occurring ``error``)
- an ``error`` with no ``fail`` → nonzero but a *distinct* code (a check that could not run
  is not a passed contract; a gate must not go green on it).
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from kdive.cli.commands import doctor

_FAIL_CODE = 1
_ERROR_CODE = 6


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
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def client(self) -> _FakeClient:
        return self._client


def _install_session(monkeypatch: pytest.MonkeyPatch, payload: dict) -> _FakeClient:
    client = _FakeClient(payload)
    monkeypatch.setattr(doctor, "_session_factory", lambda: _FakeSession(client))
    return client


def _check(check_id: str, status: str, detail: str, fix=None, provider=None) -> dict:
    return {
        "object_id": check_id,
        "status": "ok",
        "items": [],
        "data": {
            "check": check_id,
            "status": status,
            "detail": detail,
            "fix": fix,
            "provider": provider,
        },
    }


def _verdict(checks: list[dict], *, has_failure: bool, has_error: bool) -> dict:
    return {
        "object_id": "diagnostics",
        "status": "ok",
        "items": checks,
        "data": {
            "has_failure": "true" if has_failure else "false",
            "has_error": "true" if has_error else "false",
        },
    }


def _args(**kwargs: object) -> argparse.Namespace:
    kwargs.setdefault("json", False)
    kwargs.setdefault("provider", None)
    kwargs.setdefault("with_egress", False)
    return argparse.Namespace(**kwargs)


def test_all_pass_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check("secret_ref", "pass", "all refs resolve"),
                _check("provider_tls", "pass", "chain validates", provider="local-libvirt"),
            ],
            has_failure=False,
            has_error=False,
        ),
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == 0


def test_any_fail_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check("secret_ref", "pass", "ok"),
                _check(
                    "gdbstub_acl",
                    "fail",
                    "range blocked",
                    fix="open the ACL",
                    provider="remote-libvirt",
                ),
            ],
            has_failure=True,
            has_error=False,
        ),
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == _FAIL_CODE


def test_error_exits_nonzero_distinct_from_fail(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check("secret_ref", "pass", "ok"),
                _check("provider_tls", "error", "host unreachable", provider="remote-libvirt"),
            ],
            has_failure=False,
            has_error=True,
        ),
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == _ERROR_CODE
    assert _ERROR_CODE != _FAIL_CODE


def test_fail_dominates_when_fail_and_error_mix(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check("secret_ref", "fail", "ref missing", fix="create the ref"),
                _check("provider_tls", "error", "host unreachable", provider="x"),
            ],
            has_failure=True,
            has_error=True,
        ),
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == _FAIL_CODE


def test_all_error_exits_error_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [_check("secret_ref", "error", "backend unreachable")],
            has_failure=False,
            has_error=True,
        ),
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == _ERROR_CODE


def test_empty_verdict_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # No checks ran: no fail and no error, so the gate is not failed (degenerate but defined).
    _install_session(monkeypatch, _verdict([], has_failure=False, has_error=False))
    code = asyncio.run(doctor.doctor(_args()))
    assert code == 0


def test_authorization_denied_envelope_maps_to_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The operator gate denies by *returning* a failure envelope, not raising (ADR-0089).
    _install_session(
        monkeypatch,
        {
            "object_id": "diagnostics",
            "status": "error",
            "error_category": "authorization_denied",
            "items": [],
            "data": {},
        },
    )
    code = asyncio.run(doctor.doctor(_args()))
    assert code == 3


def test_table_renders_status_detail_fix_and_provider(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check(
                    "gdbstub_acl",
                    "fail",
                    "range blocked",
                    fix="open the ACL",
                    provider="remote-libvirt",
                )
            ],
            has_failure=True,
            has_error=False,
        ),
    )
    asyncio.run(doctor.doctor(_args()))
    out = capsys.readouterr().out
    assert "check" in out and "status" in out and "detail" in out
    assert "fix" in out and "provider" in out
    assert "gdbstub_acl" in out and "fail" in out and "range blocked" in out
    assert "open the ACL" in out and "remote-libvirt" in out


def test_table_shows_all_three_states(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [
                _check("secret_ref", "pass", "all refs resolve"),
                _check("gdbstub_acl", "fail", "blocked", fix="open it", provider="p"),
                _check("provider_tls", "error", "unreachable", provider="p"),
            ],
            has_failure=True,
            has_error=True,
        ),
    )
    asyncio.run(doctor.doctor(_args()))
    out = capsys.readouterr().out
    assert "pass" in out and "fail" in out and "error" in out


def test_json_mode_emits_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _verdict(
            [_check("secret_ref", "pass", "ok")],
            has_failure=False,
            has_error=False,
        ),
    )
    asyncio.run(doctor.doctor(_args(json=True)))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == [
        {
            "check": "secret_ref",
            "status": "pass",
            "detail": "ok",
            "fix": None,
            "provider": None,
        }
    ]


def test_default_run_passes_no_provider_and_no_egress(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _verdict([], has_failure=False, has_error=False))
    asyncio.run(doctor.doctor(_args()))
    assert client.calls == [("ops.diagnostics", {})]


def test_provider_target_is_threaded_to_the_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _verdict([], has_failure=False, has_error=False))
    asyncio.run(doctor.doctor(_args(provider="remote-libvirt")))
    assert client.calls == [("ops.diagnostics", {"provider": "remote-libvirt"})]


def test_with_egress_flag_is_threaded_to_the_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _verdict([], has_failure=False, has_error=False))
    asyncio.run(doctor.doctor(_args(with_egress=True)))
    assert client.calls == [("ops.diagnostics", {"with_egress": True})]


def test_doctor_is_a_known_subcommand() -> None:
    from kdive.cli.__main__ import build_parser

    args = build_parser().parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.provider is None and args.with_egress is False


def test_doctor_parses_provider_and_egress_flags() -> None:
    from kdive.cli.__main__ import build_parser

    args = build_parser().parse_args(["doctor", "--provider", "remote-libvirt", "--with-egress"])
    assert args.provider == "remote-libvirt" and args.with_egress is True


def test_doctor_json_flag_accepted_after_the_verb() -> None:
    from kdive.cli.__main__ import build_parser

    args = build_parser().parse_args(["doctor", "--json"])
    assert args.json is True


def test_dispatch_routes_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.cli import dispatch
    from kdive.cli.__main__ import build_parser

    seen: list[argparse.Namespace] = []

    async def _fake(args: argparse.Namespace) -> int:
        seen.append(args)
        return 7

    monkeypatch.setattr(dispatch.commands.doctor, "doctor", _fake)
    args = build_parser().parse_args(["doctor"])
    assert asyncio.run(dispatch.run(args)) == 7
    assert seen and seen[0].command == "doctor"


def test_untrusted_detail_is_rendered_on_one_logical_row(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A newline injected into a tool-returned detail must not forge extra table rows.
    _install_session(
        monkeypatch,
        _verdict(
            [_check("secret_ref", "fail", "evil\nrow: injected", fix="fix\nme")],
            has_failure=True,
            has_error=False,
        ),
    )
    asyncio.run(doctor.doctor(_args()))
    out = capsys.readouterr().out
    assert "\nrow: injected" not in out
    assert "evil" in out
