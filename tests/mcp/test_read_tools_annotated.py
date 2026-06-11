"""Every domain read tool carries ``readOnlyHint=True`` so the passthrough can reach it.

The generic ``kdivectl tool call`` passthrough fail-closed-gates on ``readOnlyHint``
(ADR-0089). A domain read tool that forgets ``annotations=_docmeta.read_only()`` is
therefore unreachable without a curated verb. This guard holds every such tool to the
hint, making the milestone's "lists/inspects every domain object" claim falsifiable.

``secrets.list`` and ``fixtures.list`` (the #252 net-new read tools) carry the same hint:
both are domain reads reachable through the passthrough, so they are guarded here too.
"""

from __future__ import annotations

import asyncio

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.cli.commands import REGISTRY
from kdive.mcp.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

READ_TOOLS = [
    "resources.list",
    "resources.describe",
    "allocations.list",
    "allocations.get",
    "systems.list",
    "systems.get",
    "runs.get",
    "jobs.list",
    "jobs.get",
    "accounting.usage_project",
    "inventory.list",
    "secrets.list",
    "fixtures.list",
    "images.list",
]


def _verifier() -> JWTVerifier:
    keypair = make_keypair()
    return JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)


def _tools_by_name() -> dict[str, object]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _collect() -> dict[str, object]:
        return {tool.name: tool for tool in await app.list_tools()}

    return asyncio.run(_collect())


def _is_read_only(tool: object) -> bool:
    return getattr(getattr(tool, "annotations", None), "readOnlyHint", None) is True


def test_read_tools_carry_read_only_hint() -> None:
    tools = _tools_by_name()
    missing = [name for name in READ_TOOLS if name not in tools]
    assert not missing, f"read tools not registered: {missing}"
    not_annotated = [name for name in READ_TOOLS if not _is_read_only(tools[name])]
    assert not not_annotated, f"read tools unreachable via passthrough: {not_annotated}"


def test_every_curated_read_verb_targets_a_read_only_tool() -> None:
    # Derive the guarded set from the SAME registry that drives dispatch, so a future verb
    # wired to a mutating tool fails here instead of silently reaching it (ADR-0089).
    tools = _tools_by_name()
    offenders = [
        verb.tool
        for verb in REGISTRY
        if verb.read_only and (verb.tool not in tools or not _is_read_only(tools[verb.tool]))
    ]
    assert not offenders, f"curated read verbs target non-read-only tools: {offenders}"


def test_mutating_verbs_target_non_read_only_tools() -> None:
    # The dual of the read-only guard: a verb declared mutating MUST target a tool that is
    # provably not read-only, so the read-only passthrough can never reach it (ADR-0089).
    tools = _tools_by_name()
    leaks = [
        verb.tool
        for verb in REGISTRY
        if not verb.read_only and (verb.tool not in tools or _is_read_only(tools[verb.tool]))
    ]
    assert not leaks, f"mutating verbs target read-only/unknown tools: {leaks}"
