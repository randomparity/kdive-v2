"""The dedicated auxiliary health/metrics HTTP listener (ADR-0090 §5).

All three processes expose ``/livez``, ``/readyz``, ``/metrics`` on this listener —
**distinct from the server's public MCP port** and **bound loopback / pod-local by
default**. The endpoints carry no authentication of their own, so the network boundary
*is* their access control: an unauthenticated ``/readyz`` that triggers backend calls
must not be reachable by arbitrary clients. The bind address is a validated config key
(:data:`kdive.config.core_settings.HEALTH_BIND_ADDR`) with a loopback default, so
widening the boundary is an explicit, reviewed act, not implementation memory.
"""

from __future__ import annotations

import uvicorn
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from kdive.health.heartbeat import Heartbeat
from kdive.health.metrics_text import CONTENT_TYPE, render_prometheus
from kdive.health.probe import HealthProbe


def build_aux_app(
    *,
    heartbeat: Heartbeat,
    probe: HealthProbe,
    metric_reader: InMemoryMetricReader | None,
) -> Starlette:
    """Build the aux Starlette app exposing ``/livez``, ``/readyz``, ``/metrics``.

    Args:
        heartbeat: The loop heartbeat ``/livez`` reads (affirmative liveness, §5).
        probe: The readiness probe ``/readyz`` gates on (this process's dependency set).
        metric_reader: The in-memory reader ``/metrics`` scrapes; ``None`` makes
            ``/metrics`` return 404 (a process running without a scrape reader).
    """

    async def livez(_: Request) -> Response:
        live = heartbeat.is_live()
        return PlainTextResponse("ok" if live else "stale", status_code=_code(live))

    async def readyz(_: Request) -> Response:
        result = await probe.check()
        return JSONResponse(
            {"ready": result.ready, "checks": result.checks},
            status_code=_code(result.ready),
        )

    async def metrics(_: Request) -> Response:
        if metric_reader is None:
            return PlainTextResponse("no metric reader configured", status_code=404)
        body = render_prometheus(metric_reader.get_metrics_data())
        return PlainTextResponse(body, media_type=CONTENT_TYPE)

    return Starlette(
        routes=[
            Route("/livez", livez),
            Route("/readyz", readyz),
            Route("/metrics", metrics),
        ]
    )


def _code(ok: bool) -> int:
    return 200 if ok else 503


async def serve_aux(
    app: Starlette,
    *,
    host: str,
    port: int,
) -> None:
    """Serve the aux app with uvicorn on ``host:port`` until cancelled.

    The caller is responsible for supplying a loopback/pod-local ``host`` (resolved from
    :data:`kdive.config.core_settings.HEALTH_BIND_ADDR`); this function does not widen it.
    """
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    await server.serve()
