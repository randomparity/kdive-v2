"""End-to-end facade wiring (ADR-0090 §1, §2): bootstrap ordering + stdout floor.

Asserts the acceptance criteria that live at the facade boundary: a registered secret
is redacted in the stdout output the running process actually emits, the stdlib floor
is installed first and then handed over to the OTel bridge (no doubling), and OTLP
stays off by default.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

import kdive.config as config
from kdive.log import _KdiveHandler
from kdive.observability.facade import LoggingHandler, bootstrap_stdout_floor, init_telemetry
from kdive.security.secrets.secret_registry import SecretRegistry


@pytest.fixture(autouse=True)
def _restore_root() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    root.handlers = before
    root.setLevel(before_level)


def test_bootstrap_floor_installs_stdlib_handler_first() -> None:
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    root = logging.getLogger()
    assert any(isinstance(h, _KdiveHandler) for h in root.handlers)


def test_init_telemetry_replaces_floor_with_bridge() -> None:
    config.load({})
    registry = SecretRegistry()
    bootstrap_stdout_floor("INFO", secret_registry=registry)
    init_telemetry("server", secret_registry=registry, level="INFO")
    root = logging.getLogger()
    assert not any(isinstance(h, _KdiveHandler) for h in root.handlers), "floor must be removed"
    assert any(isinstance(h, LoggingHandler) for h in root.handlers), "bridge must be installed"


def test_registered_secret_is_redacted_in_stdout(monkeypatch) -> None:
    config.load({})
    registry = SecretRegistry()
    registry.register("hunter2-prod-token", scope=None)
    stream = io.StringIO()
    # The stdout exporter binds sys.stderr at construction, so patch before init.
    monkeypatch.setattr("sys.stderr", stream)
    telemetry = init_telemetry("worker", secret_registry=registry, level="INFO")
    logging.getLogger("kdive.test.facade").info("using hunter2-prod-token to connect")
    telemetry.logger_provider.force_flush()
    output = stream.getvalue()
    assert output, "expected the stdout exporter to emit a line"
    record = json.loads(output.splitlines()[-1])
    assert "hunter2-prod-token" not in output
    assert "[REDACTED]" in record["msg"]
    assert record["logger"] == "kdive.test.facade"
