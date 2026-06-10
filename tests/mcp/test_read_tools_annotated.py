"""Every domain read tool carries ``readOnlyHint=True`` so the passthrough can reach it.

The generic ``kdivectl tool call`` passthrough fail-closed-gates on ``readOnlyHint``
(ADR-0089). A domain read tool that forgets ``annotations=_docmeta.read_only()`` is
therefore unreachable without a curated verb. This guard holds every such tool to the
hint, making the milestone's "lists/inspects every domain object" claim falsifiable.

#252 extends ``READ_TOOLS`` with ``secrets.list`` and ``fixtures.list`` once those net-new
read tools land; they are intentionally absent here because they do not exist on this branch.
"""

from __future__ import annotations

import asyncio

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

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


def test_read_tools_carry_read_only_hint() -> None:
    tools = _tools_by_name()
    missing = [name for name in READ_TOOLS if name not in tools]
    assert not missing, f"read tools not registered: {missing}"
    not_annotated = [
        name
        for name in READ_TOOLS
        if getattr(getattr(tools[name], "annotations", None), "readOnlyHint", None) is not True
    ]
    assert not not_annotated, f"read tools unreachable via passthrough: {not_annotated}"
