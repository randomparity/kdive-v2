"""Run-local campaign driver (gitignored): mint a role token, call one MCP tool, print envelope.

Usage:
  uv run python artifacts/coverage-campaign/drive.py \
     --base http://127.0.0.1:8000/mcp --project demo --subject agent \
     --role operator --platform-roles platform_admin \
     --tool resources.list --args '{}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from tests.integration.live_stack.harness import LiveStackClient, mint_token, oidc_issuer_from_env


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--project", default="demo")
    p.add_argument("--subject", default="campaign-agent")
    p.add_argument("--role", default=None, help="project role: viewer|operator|admin")
    p.add_argument("--platform-roles", default="", help="csv of platform roles")
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}", help="JSON object of tool args")
    return p.parse_args()


async def _run(ns: argparse.Namespace) -> int:
    issuer = oidc_issuer_from_env()
    roles = {ns.project: ns.role} if ns.role else {}
    platform_roles = [r for r in ns.platform_roles.split(",") if r]
    token = mint_token(
        issuer,
        subject=ns.subject,
        projects=[ns.project],
        roles=roles,
        platform_roles=platform_roles or None,
    )
    args = json.loads(ns.args)
    try:
        async with LiveStackClient.over_http(ns.base, token) as client:
            resp = await client.call_tool(ns.tool, **args)
    except Exception as exc:  # noqa: BLE001 - campaign driver surfaces any failure verbatim
        print(json.dumps({"driver_error": type(exc).__name__, "message": str(exc)}))
        return 2
    payload = resp if isinstance(resp, list) else [resp]
    rendered = [p.model_dump(mode="json") if hasattr(p, "model_dump") else p for p in payload]
    print(json.dumps(rendered, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run(_parse())))
