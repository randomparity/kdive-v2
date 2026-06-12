"""``kdivectl images`` verbs call the right server tool with the expected payload.

The verbs are driven through fakes for the MCP client so the tests are hermetic. ``list``
is a read passthrough; ``upload``/``delete``/``build``/``publish``/``prune``/``extend`` are
mutating verbs that run the fail-closed token preflight first, then call their server tool
(ADR-0089). A denial envelope from the server maps to exit ``3``.
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

import kdive.cli.commands.images as images
import kdive.cli.commands.mutations as mutations
import kdive.cli.commands.reads as reads
from kdive.cli.commands.registry import REGISTRY


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


def _install(monkeypatch: pytest.MonkeyPatch, payload: dict | None = None) -> _FakeClient:
    client = _FakeClient(payload or {"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))
    monkeypatch.setattr(mutations, "ensure_token_valid", lambda *a, **k: None)
    return client


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=False, **kwargs)


def _collection(items: list[dict]) -> dict:
    return {
        "object_id": "images",
        "status": "ok",
        "data": {"count": str(len(items))},
        "items": items,
    }


def test_list_calls_images_list_read_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(
        monkeypatch,
        _collection(
            [{"object_id": "i1", "status": "registered", "data": {"name": "fedora"}, "items": []}]
        ),
    )
    code = asyncio.run(images.images_list(_args()))
    assert code == 0
    assert client.calls == [("images.list", {})]
    assert "fedora" in capsys.readouterr().out


def test_upload_calls_images_upload_with_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_upload(
            _args(
                project="proj-a",
                name="custom",
                arch="x86_64",
                quarantine_key="quarantine/abc",
                lifetime_seconds=3600,
            )
        )
    )
    assert client.calls == [
        (
            "images.upload",
            {
                "project": "proj-a",
                "name": "custom",
                "arch": "x86_64",
                "quarantine_key": "quarantine/abc",
                "lifetime_seconds": 3600,
            },
        )
    ]


def test_upload_omits_lifetime_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_upload(
            _args(
                project="proj-a",
                name="custom",
                arch="x86_64",
                quarantine_key="quarantine/abc",
                lifetime_seconds=None,
            )
        )
    )
    assert client.calls == [
        (
            "images.upload",
            {
                "project": "proj-a",
                "name": "custom",
                "arch": "x86_64",
                "quarantine_key": "quarantine/abc",
            },
        )
    ]


def test_delete_calls_images_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert client.calls == [("images.delete", {"image_id": "img-1"})]


def test_build_calls_images_build(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_build(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                arch="x86_64",
                releasever="40",
                source_image_digest="sha256:base",
                capabilities="agent,kdump",
            )
        )
    )
    assert client.calls == [
        (
            "images.build",
            {
                "request": {
                    "provider": "local-libvirt",
                    "name": "fedora-40",
                    "arch": "x86_64",
                    "releasever": "40",
                    "source_image_digest": "sha256:base",
                    "capabilities": ["agent", "kdump"],
                },
            },
        )
    ]


def test_build_trims_blank_capability_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_build(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                arch="x86_64",
                releasever="40",
                source_image_digest="sha256:base",
                capabilities=" agent, ,kdump, ",
            )
        )
    )
    assert client.calls[0] == (
        "images.build",
        {
            "request": {
                "provider": "local-libvirt",
                "name": "fedora-40",
                "arch": "x86_64",
                "releasever": "40",
                "source_image_digest": "sha256:base",
                "capabilities": ["agent", "kdump"],
            },
        },
    )


def test_publish_calls_images_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_publish(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                arch="x86_64",
                releasever="40",
                source_image_digest="sha256:base",
                capabilities="agent",
            )
        )
    )
    assert client.calls[0][0] == "images.publish"


def test_prune_expired_calls_break_glass_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_prune(_args(expired=True, reason="cleanup")))
    assert client.calls == [("images.prune_expired", {"reason": "cleanup"})]


def test_prune_requires_expired_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    with pytest.raises(SystemExit):
        asyncio.run(images.images_prune(_args(expired=False, reason="x")))
    assert client.calls == []


def test_extend_calls_images_extend(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_extend(_args(image_id="img-1", seconds=86400, reason="keep")))
    assert client.calls == [
        ("images.extend", {"image_id": "img-1", "seconds": 86400, "reason": "keep"})
    ]


def test_denied_envelope_maps_to_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        payload={
            "object_id": "img-1",
            "status": "error",
            "error_category": "authorization_denied",
            "data": {},
        },
    )
    code = asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert code == 3


def test_mutating_image_verbs_run_preflight_first(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))

    class _Boom(RuntimeError):
        pass

    def _refuse(*_a: object, **_k: object) -> None:
        raise _Boom

    monkeypatch.setattr(mutations, "ensure_token_valid", _refuse)
    with pytest.raises(_Boom):
        asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert client.calls == []


def test_image_verbs_registered_with_expected_read_only_flags() -> None:
    by_tool = {verb.tool: verb for verb in REGISTRY if verb.group == "images"}
    assert by_tool["images.list"].read_only is True
    for mutating in (
        "images.upload",
        "images.delete",
        "images.build",
        "images.publish",
        "images.prune_expired",
        "images.extend",
    ):
        assert by_tool[mutating].read_only is False
