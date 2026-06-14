"""The flat-outputSchema sweep that fixes the recursive ToolResponse schema (#404, ADR-0113)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from kdive.mcp.app import ENVELOPE_OUTPUT_SCHEMA, _advertise_flat_output_schema
from kdive.mcp.responses import ToolResponse


def _probe_app() -> FastMCP:
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="scalar.one")
    def scalar_one() -> ToolResponse:
        return ToolResponse.success("obj-1", "ok", data={"k": "v"})

    @app.tool(name="list.coll")
    def list_coll() -> ToolResponse:
        return ToolResponse.collection("c", "ok", [ToolResponse.success("a", "ok")])

    return app


class _ErrorCollector(logging.Handler):
    """Capture ERROR records off the ``fastmcp`` logger.

    The FastMCP client logger sets ``propagate=False`` and uses its own handler, so pytest's
    ``caplog`` (a root-logger handler) does NOT see the parse error — verified. Attach directly.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _call_and_capture(app: FastMCP, tool: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Call ``tool`` on ``app``; return (``.data``, structured-content parse-error messages)."""
    logger = logging.getLogger("fastmcp")
    handler = _ErrorCollector()
    logger.addHandler(handler)
    try:

        async def _call() -> dict[str, Any] | None:
            async with Client(app) as client:
                result = await client.call_tool(tool, {})
                return result.data

        data = asyncio.run(_call())
    finally:
        logger.removeHandler(handler)
    errors = [r.getMessage() for r in handler.records if "structured content" in r.getMessage()]
    return data, errors


def test_sweep_advertises_flat_object_schema() -> None:
    app = _probe_app()
    swept = _advertise_flat_output_schema(app)
    assert swept == 2

    async def _run() -> list[dict[str, object] | None]:
        async with Client(app) as client:
            return [t.outputSchema for t in await client.list_tools()]

    schemas = asyncio.run(_run())
    assert schemas == [ENVELOPE_OUTPUT_SCHEMA, ENVELOPE_OUTPUT_SCHEMA]


def test_sweep_restores_data_and_logs_no_parse_error() -> None:
    app = _probe_app()
    _advertise_flat_output_schema(app)
    data, errors = _call_and_capture(app, "scalar.one")
    assert isinstance(data, dict)
    assert data["object_id"] == "obj-1"  # S1b: .data restored
    assert errors == []  # S1a: no parse-error log


def test_unswept_recursive_schema_fails_to_parse() -> None:
    """Regression pin: without the sweep the auto-derived recursive schema breaks the client.

    Pinned to fastmcp 3.4.0 client behavior; a major FastMCP upgrade that handles recursive
    ``$ref`` would make this auto-schema parse cleanly and is the expected reason to revisit it.
    """
    app = _probe_app()  # NOT swept
    data, errors = _call_and_capture(app, "scalar.one")
    assert data is None  # the failed validator nulls .data
    assert errors  # the parse error is logged


def test_sweep_raises_on_empty_tool_surface() -> None:
    """A zero count means the registry accessor broke — fail loud, don't ship recursive schemas."""
    empty: FastMCP = FastMCP(name="empty")
    with pytest.raises(RuntimeError):
        _advertise_flat_output_schema(empty)
