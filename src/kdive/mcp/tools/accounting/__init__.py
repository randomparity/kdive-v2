"""Accounting MCP tool registrar and compatibility exports."""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.accounting import admin, reports, usage
from kdive.mcp.tools.accounting import estimate as estimate_tools
from kdive.mcp.tools.accounting.admin import set_budget, set_quota
from kdive.mcp.tools.accounting.estimate import estimate
from kdive.mcp.tools.accounting.reports import report_all_projects, report_granted_set
from kdive.mcp.tools.accounting.usage import usage_investigation, usage_project

__all__ = [
    "estimate",
    "register",
    "report_all_projects",
    "report_granted_set",
    "set_budget",
    "set_quota",
    "usage_investigation",
    "usage_project",
]


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register all ``accounting.*`` tools on ``app``, bound to ``pool``."""
    estimate_tools.register(app, pool)
    usage.register(app, pool)
    reports.register(app, pool)
    admin.register(app, pool)
