"""On-demand reconcile MCP tool (``ops.reconcile_now``) — ADR-0062 §reconcile, M1.3.

``ops.reconcile_now`` runs one :func:`kdive.reconciler.loop.reconcile_once` pass on
demand and returns its per-class repair summary. It calls the **same** ``reconcile_once``
the periodic loop runs (:mod:`kdive.reconciler.loop`), so it inherits that pass's
per-Project / per-Allocation / per-System ``advisory_xact_lock`` discipline unchanged:
there is no second, lock-free repair path. An on-demand pass and a concurrent periodic
pass therefore serialize on the same advisory locks and cannot double-act on one object.
It does **not** stop or restart the periodic loop — it triggers one extra pass.

Gated ``platform_operator`` (a cross-project control action) and audited to
``platform_audit_log``.
"""

from __future__ import annotations

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.ops._auth import audit_platform_denial, held_platform_roles
from kdive.providers.reaping import InfraReaper
from kdive.reconciler.loop import ReconcileReport, UploadStore, reconcile_once
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_RECONCILE_TOOL = "ops.reconcile_now"
_RECONCILE_OBJECT_ID = "reconcile"
# A control action over every project, not one project/object (ADR-0062 §reconcile).
_RECONCILE_SCOPE = "all-projects"


async def reconcile_now(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    reaper: InfraReaper,
    upload_store: UploadStore | None,
) -> ToolResponse:
    """Run one ``reconcile_once`` pass on demand; return its per-class repair summary.

    Gates ``platform_operator`` first (a denial writes no row and never touches the pool),
    then runs the **same** advisory-locked ``reconcile_once`` as the periodic loop, audits
    the action to ``platform_audit_log``, and returns the counts per repair class plus the
    names of any repairs that raised this pass.

    Args:
        pool: The shared async pool ``reconcile_once`` draws a fresh connection per repair
            from — the same pool the periodic reconciler uses, so the two passes share the
            advisory locks.
        ctx: The caller's request context; must hold ``platform_operator``.
        reaper: The infra reaper the leaked-domain repair consumes; registration resolves
            it through the same provider composition seam as the periodic loop.
        upload_store: The object store the abandoned-upload reaper consumes, or ``None`` to
            skip that repair (mirrors the periodic loop when ``KDIVE_S3_*`` is unconfigured).

    Returns:
        A success ``ToolResponse`` carrying the per-class counts and ``failures`` list, or a
        ``ToolResponse.failure(AUTHORIZATION_DENIED)`` when the caller lacks the role.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_RECONCILE_TOOL,
                scope=_RECONCILE_SCOPE,
                args={"tool": _RECONCILE_TOOL},
            )
            return ToolResponse.failure(
                _RECONCILE_OBJECT_ID,
                ErrorCategory.AUTHORIZATION_DENIED,
                suggested_next_actions=[_RECONCILE_TOOL],
            )
        # reconcile_once isolates every per-repair failure into report.failures and does
        # not re-raise it, so there is no CategorizedError to catch here; a rare whole-pass
        # error (e.g. pool acquisition) propagates, matching the periodic loop's contract.
        report = await reconcile_once(pool, reaper, upload_store=upload_store)
        async with pool.connection() as conn, conn.transaction():
            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=_RECONCILE_TOOL,
                    scope=_RECONCILE_SCOPE,
                    args={"tool": _RECONCILE_TOOL},
                    platform_role=held_platform_roles(ctx),
                ),
            )
        return _reconcile_response(report)


def _reconcile_response(report: ReconcileReport) -> ToolResponse:
    """Render a :class:`ReconcileReport` as a per-class summary ``ToolResponse``."""
    return ToolResponse.success(
        _RECONCILE_OBJECT_ID,
        "ok",
        suggested_next_actions=["ops.reconcile_now"],
        data={
            "expired_allocations": str(report.expired_allocations),
            "promoted_allocations": str(report.promoted_allocations),
            "queue_timeouts": str(report.queue_timeouts),
            "orphaned_systems": str(report.orphaned_systems),
            "abandoned_jobs": str(report.abandoned_jobs),
            "dead_sessions": str(report.dead_sessions),
            "leaked_domains": str(report.leaked_domains),
            "idempotency_keys_gc_count": str(report.idempotency_keys_gc_count),
            "abandoned_uploads": str(report.abandoned_uploads),
            "failures": ",".join(report.failures),
        },
    )


def _resolve_upload_store() -> UploadStore | None:
    """Resolve the upload store from the ``KDIVE_S3_*`` env, or ``None`` if unconfigured.

    Mirrors the periodic reconciler's wiring (``kdive.__main__._run_reconciler``): no S3
    env means the abandoned-upload reaper stays off, exactly as it does for the periodic
    pass, so the on-demand pass repairs the same set the periodic loop does.
    """
    from kdive.store.objectstore import object_store_from_env

    try:
        return object_store_from_env()
    except CategorizedError:
        return None


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register ``ops.reconcile_now`` on ``app``, bound to ``pool``.

    The reaper and upload store are resolved once at registration — the same construction
    the periodic reconciler uses — so the on-demand pass and the periodic loop run an
    identical ``reconcile_once``.
    """
    from kdive.providers.composition import build_reconciler_reaper

    reaper = build_reconciler_reaper()
    upload_store = _resolve_upload_store()

    @app.tool(
        name=_RECONCILE_TOOL,
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def ops_reconcile_now() -> ToolResponse:
        """Run one reconcile pass on demand; return the repair summary. Platform operator."""
        return await reconcile_now(
            pool, current_context(), reaper=reaper, upload_store=upload_store
        )
