"""Shared job attribution helpers for MCP tools and worker handlers."""

from __future__ import annotations

from kdive.domain.models import Job
from kdive.jobs.payloads import Authorizing, load_authorizing
from kdive.security.context import RequestContext


def authorizing(ctx: RequestContext, project: str) -> Authorizing:
    return Authorizing(principal=ctx.principal, agent_session=ctx.agent_session, project=project)


def context_from_job(job: Job, project: str) -> RequestContext:
    auth = load_authorizing(job)
    return RequestContext(
        principal=auth.principal,
        agent_session=auth.agent_session,
        projects=(project,),
        roles={},
    )
