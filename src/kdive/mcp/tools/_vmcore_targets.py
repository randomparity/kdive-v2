"""Shared Run → build → vmcore target resolution for MCP read tools."""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.artifact_queries import raw_vmcore_key
from kdive.db.repositories import RUNS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.services.runs.steps import existing_build_result


class RunVmcoreTarget(NamedTuple):
    """The resolved inputs needed to analyze a Run's captured vmcore."""

    debuginfo_ref: str
    build_id: str
    vmcore_ref: str


async def resolve_run_vmcore_target(
    conn: AsyncConnection, ctx: RequestContext, run_id: str
) -> RunVmcoreTarget:
    """Resolve debuginfo ref, build-id, and raw vmcore key for a viewer-authorized Run.

    A malformed ``run_id`` is a parse failure (``configuration_error``). A syntactically valid
    id that resolves to no visible Run — absent, in an ungranted project (no-leak), or missing a
    prerequisite target artifact (null ``debuginfo_ref``, no recorded build, no captured core) —
    is ``not_found`` (ADR-0097). The two helpers stay distinct so the malformed branch cannot
    drift into ``not_found``.
    """
    uid = _as_uuid(run_id)
    if uid is None:
        raise _target_config_error()
    run = await RUNS.get(conn, uid)
    if run is None or run.project not in ctx.projects:
        raise _target_not_found()
    require_role(ctx, run.project, Role.VIEWER)
    if run.debuginfo_ref is None:
        raise _target_not_found()
    build_id = await _build_id_for_run(conn, uid)
    if build_id is None:
        raise _target_not_found()
    vmcore_ref = await raw_vmcore_key(conn, run.system_id)
    if vmcore_ref is None:
        raise _target_not_found()
    return RunVmcoreTarget(run.debuginfo_ref, build_id, vmcore_ref)


def _target_config_error() -> CategorizedError:
    return CategorizedError(
        "run_id is not a uuid",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


def _target_not_found() -> CategorizedError:
    return CategorizedError(
        "run does not resolve to a captured vmcore target",
        category=ErrorCategory.NOT_FOUND,
    )


async def _build_id_for_run(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    return None if result is None else result.build_id
