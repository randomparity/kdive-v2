"""Closed actor-attribution map for audit rows (ADR-0089 decision 5).

Resolves the *caller class* recorded in ``platform_audit_log.actor`` from the OIDC
client id and agent-session presence. The map is closed and fail-safe: anything that is
neither the configured CLI client nor a recognised agent-with-session is ``unknown`` —
never silently attributed to ``agent``, so an unrecognised caller cannot masquerade as a
trusted agent in the audit trail.
"""

from __future__ import annotations

OPERATOR_CLI = "operator-cli"
AGENT = "agent"
UNKNOWN = "unknown"


def resolve_actor(client_id: str | None, *, agent_session: str | None, cli_client_id: str) -> str:
    """Return the audit ``actor`` class for a verified token.

    Args:
        client_id: The token's OIDC ``azp``/``client_id`` claim, or ``None`` when absent.
        agent_session: The token's ``agent_session`` claim, or ``None``.
        cli_client_id: The configured operator-CLI client id to match ``client_id`` against.

    Returns:
        ``"operator-cli"`` when ``client_id`` is the configured CLI client (authoritative,
        regardless of session); ``"agent"`` when an ``agent_session`` is present; otherwise
        ``"unknown"`` — the fail-safe that never defaults an unrecognised caller to ``agent``.
    """
    if client_id is not None and client_id == cli_client_id:
        return OPERATOR_CLI
    if agent_session:
        return AGENT
    return UNKNOWN
