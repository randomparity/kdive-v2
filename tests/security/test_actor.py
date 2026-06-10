"""Tests for the closed actor-attribution map (ADR-0089 decision 5).

`resolve_actor` classifies an audited caller from the verified token's OIDC
``client_id`` and ``agent_session`` presence. The map is closed and fail-safe:
anything that is neither the configured CLI client nor a recognised
agent-with-session is ``unknown`` — never silently attributed to ``agent``.
"""

from __future__ import annotations

from kdive.security.authz.actor import resolve_actor

KDIVE_CLI = "kdivectl"
AGENT_CLIENT = "kdive-agent"


def test_kdivectl_client_is_operator_cli() -> None:
    assert resolve_actor(KDIVE_CLI, agent_session=None, cli_client_id=KDIVE_CLI) == "operator-cli"


def test_kdivectl_client_is_operator_cli_even_with_session() -> None:
    # The CLI client id is authoritative; a stray agent_session does not downgrade it.
    actor = resolve_actor(KDIVE_CLI, agent_session="sess-1", cli_client_id=KDIVE_CLI)
    assert actor == "operator-cli"


def test_agent_client_with_session_is_agent() -> None:
    assert resolve_actor(AGENT_CLIENT, agent_session="sess-1", cli_client_id=KDIVE_CLI) == "agent"


def test_unrecognised_is_unknown_never_agent() -> None:
    # Neither the CLI client nor an agent-with-session → unknown, never defaulting to agent.
    assert resolve_actor("mystery", agent_session=None, cli_client_id=KDIVE_CLI) == "unknown"
    assert resolve_actor(None, agent_session=None, cli_client_id=KDIVE_CLI) == "unknown"


def test_missing_client_with_session_is_agent() -> None:
    # No client_id but a session present → an agent caller (agent tokens omit azp).
    assert resolve_actor(None, agent_session="sess-1", cli_client_id=KDIVE_CLI) == "agent"


def test_empty_client_id_is_not_the_cli() -> None:
    # An empty client id must not match the CLI client; with no session it is unknown.
    assert resolve_actor("", agent_session=None, cli_client_id=KDIVE_CLI) == "unknown"
