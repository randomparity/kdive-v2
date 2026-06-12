"""Curated read verbs call the right tool, flatten the envelope, and render rows/records.

The verbs are driven through fakes for the MCP client so the tests are hermetic: a fake
client returns a deserialized ``ToolResponse``-shaped payload (``object_id`` + ``status``
+ ``data`` + ``items``), the verb flattens it to rows, and ``render`` prints them.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json

import pytest

import kdive.cli.commands.reads as reads
from kdive.cli.commands import REGISTRY


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
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    return client


def _collection(items: list[dict]) -> dict:
    return {"object_id": "x", "status": "ok", "data": {"count": str(len(items))}, "items": items}


def _item(object_id: str, status: str, data: dict) -> dict:
    return {"object_id": object_id, "status": status, "data": data, "items": []}


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=False, **kwargs)


def test_resources_list_flattens_items_and_renders(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("r1", "ok", {"kind": "local-libvirt", "host": "qemu:///system"})]),
    )
    code = asyncio.run(reads.resources_list(_args(kind=None)))
    assert code == 0
    assert client.calls == [("resources.list", {})]
    out = capsys.readouterr().out
    assert "r1" in out and "local-libvirt" in out and "qemu:///system" in out


def test_resources_list_passes_kind_filter(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.resources_list(_args(kind="remote-libvirt")))
    assert client.calls == [("resources.list", {"kind": "remote-libvirt"})]


def test_list_verb_id_comes_from_object_id_and_state_from_status(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _collection([_item("al-1", "active", {"project": "p", "system": "s"})]),
    )
    asyncio.run(reads.allocations_list(_args(project="p")))
    out = capsys.readouterr().out
    # id <- object_id, state <- status, project/system <- data.
    assert "al-1" in out and "active" in out and "p" in out and "s" in out


def test_allocations_list_requires_project_in_payload(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.allocations_list(_args(project="proj-a")))
    assert client.calls == [("allocations.list", {"project": "proj-a"})]


def test_resources_describe_renders_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {
        "object_id": "r1",
        "status": "ok",
        "data": {"pool": "p", "host_uri": "u"},
        "items": [],
    }
    client = _install_session(monkeypatch, record)
    code = asyncio.run(reads.resources_describe(_args(resource_id="r1")))
    assert code == 0
    assert client.calls == [("resources.describe", {"resource_id": "r1"})]
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "pool" in out and "p" in out


def test_record_verb_json_mode_emits_flat_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {"object_id": "s1", "status": "running", "data": {"project": "p"}, "items": []}
    _install_session(monkeypatch, record)
    asyncio.run(reads.systems_show(argparse.Namespace(json=True, system_id="s1")))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == "s1" and parsed["state"] == "running" and parsed["project"] == "p"


def test_ledger_show_is_a_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {"object_id": "p", "status": "ok", "data": {"kcu": "12", "window": "30d"}, "items": []}
    client = _install_session(monkeypatch, record)
    asyncio.run(reads.ledger_show(_args(project="proj-a")))
    assert client.calls == [("accounting.usage_project", {"project": "proj-a"})]
    out = capsys.readouterr().out
    assert "kcu" in out and "12" in out


def test_inventory_show_lists_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("k1", "ok", {"key": "k1", "backend": "minio", "status": "ready"})]),
    )
    asyncio.run(reads.inventory_show(_args(project=None)))
    assert client.calls == [("inventory.list", {})]
    out = capsys.readouterr().out
    assert "minio" in out and "ready" in out


def _data_envelope(data: dict) -> dict:
    return {"object_id": "x", "status": "ok", "data": data, "items": []}


def test_secrets_list_renders_refs_from_data(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # secrets.list returns refs under data.secrets (a flat string list), not nested items.
    client = _install_session(monkeypatch, _data_envelope({"secrets": ["ref://a", "ref://b"]}))
    code = asyncio.run(reads.secrets_list(_args()))
    assert code == 0
    assert client.calls == [("secrets.list", {})]
    out = capsys.readouterr().out
    assert "ref" in out and "ref://a" in out and "ref://b" in out


def test_secrets_list_json_mode_emits_ref_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(monkeypatch, _data_envelope({"secrets": ["ref://a"]}))
    asyncio.run(reads.secrets_list(argparse.Namespace(json=True)))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == [{"ref": "ref://a"}]


def test_fixtures_list_renders_rows_from_data(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _data_envelope({"fixtures": [{"provider": "local-libvirt", "name": "base", "arch": "x"}]}),
    )
    code = asyncio.run(reads.fixtures_list(_args()))
    assert code == 0
    assert client.calls == [("fixtures.list", {})]
    out = capsys.readouterr().out
    assert "local-libvirt" in out and "base" in out


def test_data_shaped_lists_ignore_malformed_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _data_envelope(
            {
                "fixtures": [
                    {"provider": "local-libvirt", "name": "base", "arch": "x86_64"},
                    "not-a-row",
                ]
            }
        ),
    )
    asyncio.run(reads.fixtures_list(_args()))
    out = capsys.readouterr().out
    assert "local-libvirt" in out
    assert "not-a-row" not in out


def test_data_shaped_lists_ignore_missing_list_data(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _data_envelope({"secrets": "not-a-list"}),  # pragma: allowlist secret - key name only
    )
    asyncio.run(reads.secrets_list(_args()))
    out = capsys.readouterr().out.strip()
    assert out and len(out.splitlines()) == 1


def test_every_registry_verb_has_a_handler() -> None:
    # The registry is the single source of truth; every entry must resolve to a callable.
    for verb in REGISTRY:
        assert callable(verb.handler)


_READ_VERBS = [v for v in REGISTRY if v.read_only]


@pytest.mark.parametrize("verb", _READ_VERBS, ids=lambda v: f"{v.group}.{v.sub}")
def test_handler_calls_the_tool_the_registry_declares(verb, monkeypatch, capsys) -> None:
    # Bind verb.tool (what the read-only gate test checks) to the handler's real call, so a
    # registry that declares a read-only tool but dispatches to another would fail here. The
    # session seam is patched on the handler's own module (read verbs live in more than one
    # command module now: reads.py and images.py).
    client = _FakeClient(_collection([]))
    handler_module = importlib.import_module(verb.handler.__module__)
    monkeypatch.setattr(handler_module, "_session_factory", lambda: _FakeSession(client))
    args = argparse.Namespace(json=False)
    for name in (*verb.positionals, *verb.options):
        setattr(args, name, f"{name}-val")
    asyncio.run(verb.handler(args))
    assert client.calls and client.calls[0][0] == verb.tool


def test_list_verb_with_empty_items_prints_only_header(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.jobs_list(_args(limit=None)))
    out = capsys.readouterr().out.strip()
    assert out and len(out.splitlines()) == 1
