"""The kdivectl exit-criterion boundary test (M2.2, ADR-0089).

The load-bearing proof that the operator CLI's break-glass mutation is both *authorized* and
*accountable*: drive ``kdivectl allocations force-release`` through the real CLI entry point
(as a subprocess, so the asserted exit code is the one a script or CI actually sees) twice
against the live stack —

1. with a ``platform_admin`` token: it succeeds, and a ``platform_audit_log`` row lands
   carrying ``tool='ops.force_release'`` and ``actor='operator-cli'`` (the CLI's OIDC
   ``client_id='kdivectl'`` resolves to ``operator-cli``);
2. with an under-privileged token that still HOLDS a platform role
   (``platform_operator`` — insufficient for the ``PLATFORM_ADMIN``-gated ``force_release``,
   but a platform role): the CLI exits ``3`` (``authorization_denied``) AND a denial
   ``platform_audit_log`` row lands, again ``actor='operator-cli'``.

CRITICAL role nuance (verified against ``mcp/tools/ops/_auth.py::audit_platform_denial``):
the under-privileged principal must hold ``platform_operator``, NOT an empty platform-role
set. ``audit_platform_denial`` returns early without writing a row when the caller holds no
platform role (the routine non-grant case), so an empty-role token would be denied but NOT
audited — failing the denial-row assertion and silently hollowing out the exit criterion.

Gated ``live_stack`` (the repo's spine-test marker, ADR-0035 §4): it needs a running kdive
stack (server/worker/reconciler + Postgres/MinIO/OIDC) plus a reachable OIDC issuer, so it
skips cleanly in normal CI and runs only against a brought-up stack (``just stack-up`` +
``just test-live-stack``). See ``docs/runbooks/kdivectl.md``.
"""

from __future__ import annotations

import asyncio
import os
import sys

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from tests.integration._seed import seed_granted_allocation
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import OidcIssuer, mint_token

_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "kdivectl-boundary"
_FORCE_RELEASE_TOOL = "ops.force_release"
_OPERATOR_CLI_ACTOR = "operator-cli"
_AUTHORIZATION_DENIED_EXIT = 3


def _require_db_url() -> str:
    """Resolve the stack's Postgres URL, or skip with the exact fix (ADR-0035 §4)."""
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (docs/runbooks/kdivectl.md)")
    return db_url


def _cli_token(issuer: OidcIssuer, *, platform_role: str) -> str:
    """Mint an operator-CLI token holding exactly ``platform_role`` (no project roles).

    ``client_id='kdivectl'`` sets the OIDC ``azp`` claim so the audited ``actor`` resolves to
    ``operator-cli`` — the attribution the exit criterion asserts on both rows.
    """
    return mint_token(
        issuer,
        subject=f"{platform_role}-cli",
        projects=[],
        roles={},
        platform_roles=[platform_role],
        client_id="kdivectl",
    )


async def _run_kdivectl(argv: list[str], *, token: str, server_url: str) -> int:
    """Run the ``kdivectl`` entry point as a subprocess; return its real exit code.

    Driving the actual entry point (not the in-process handler) is the boundary the exit
    criterion prescribes: it asserts the exit code a script or CI observes, with the token
    and server URL supplied exactly as an operator would via the environment.
    """
    env = {**os.environ, "KDIVE_TOKEN": token, "KDIVE_SERVER_URL": server_url}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kdive.cli",
        *argv,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    assert proc.returncode is not None
    return proc.returncode


async def _force_release_audit_actors(db_url: str, allocation_id: str) -> list[str]:
    """Return the ``actor`` of every ``ops.force_release`` audit row scoped to ``allocation_id``.

    Both the success path (``_record_breakglass``) and the denial path
    (``audit_platform_denial``) write a ``platform_audit_log`` row whose ``scope`` embeds the
    allocation id, so this matches both this test's own rows and no others.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT actor FROM platform_audit_log WHERE tool = %s AND scope LIKE %s ORDER BY id",
            (_FORCE_RELEASE_TOOL, f"%{allocation_id}%"),
        )
        rows = await cur.fetchall()
    return [row[0] for row in rows]


async def _drive_boundary(issuer: OidcIssuer, server_url: str, db_url: str) -> None:
    """Drive the two force-release invocations and assert their codes + audit rows."""
    admin = _cli_token(issuer, platform_role="platform_admin")
    operator = _cli_token(issuer, platform_role="platform_operator")

    async with AsyncConnectionPool(db_url, open=False) as pool:
        await pool.open()
        allocation_id = await seed_granted_allocation(pool, project=_PROJECT)

    argv = ["allocations", "force-release", allocation_id, "--reason", "boundary-test"]

    admin_code = await _run_kdivectl(argv, token=admin, server_url=server_url)
    assert admin_code == 0, "platform_admin force-release should succeed (exit 0)"
    admin_operator_cli_rows = (await _force_release_audit_actors(db_url, allocation_id)).count(
        _OPERATOR_CLI_ACTOR
    )
    assert admin_operator_cli_rows >= 1, (
        "admin success must leave a platform_audit_log row with actor=operator-cli"
    )

    operator_code = await _run_kdivectl(argv, token=operator, server_url=server_url)
    assert operator_code == _AUTHORIZATION_DENIED_EXIT, (
        "platform_operator force-release should be denied with exit 3"
    )
    denial_operator_cli_rows = (await _force_release_audit_actors(db_url, allocation_id)).count(
        _OPERATOR_CLI_ACTOR
    )
    # Assert the *delta*, not a fixed count: the denial itself must add exactly one
    # operator-cli row, independent of how many the success path wrote — an
    # under-privileged-but-platform-role denial is audited, not silent.
    assert denial_operator_cli_rows == admin_operator_cli_rows + 1, (
        "the denial must add exactly one operator-cli platform_audit_log row"
    )


@pytest.mark.live_stack
def test_force_release_succeeds_for_admin_and_denies_audited_operator() -> None:
    """Admin succeeds (audited operator-cli); platform_operator is denied exit 3 AND audited."""
    issuer = require_issuer()
    server_url = require_stack()
    db_url = _require_db_url()
    asyncio.run(_drive_boundary(issuer, server_url, db_url))
