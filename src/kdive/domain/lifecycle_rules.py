"""Shared lifecycle state sets used across MCP and worker code."""

from __future__ import annotations

from kdive.domain.state import SystemState

TERMINAL_SYSTEM_STATES = frozenset({SystemState.TORN_DOWN, SystemState.FAILED})
