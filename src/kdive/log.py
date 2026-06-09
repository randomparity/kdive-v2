"""Structured-logging foundation for the kdive platform (ADR-0014).

Configures the standard-library :mod:`logging` to emit one JSON object per record,
carrying a per-request/per-job context tuple (request id, job id, principal, object
id, transition) bound through :func:`bind_context`. The context propagates through
:class:`contextvars.ContextVar`s so it survives ``await`` boundaries and stays
isolated between concurrently in-flight asyncio tasks. No third-party dependency.

Entrypoints (server, worker, reconciler) call :func:`configure_logging` once at
startup; call sites emit through the ordinary ``logging`` API and wrap units of work
in ``with bind_context(...):`` to attach context.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import sys
from collections.abc import Generator
from contextvars import ContextVar, Token

from kdive.security.secrets.redaction import SecretRedactionFilter
from kdive.security.secrets.secret_registry import SecretRegistry

_FIELDS: tuple[str, ...] = ("request_id", "job_id", "principal", "object_id", "transition")

_CONTEXT: dict[str, ContextVar[str | None]] = {
    field: ContextVar(f"kdive_log_{field}", default=None) for field in _FIELDS
}

_CTX_RECORD_ATTR = "_kdive_ctx"


@contextlib.contextmanager
def bind_context(**fields: str) -> Generator[None]:
    """Bind log-context fields for the duration of the ``with`` block.

    Args:
        **fields: Any subset of ``request_id``, ``job_id``, ``principal``,
            ``object_id``, ``transition``. Each becomes part of every log record
            emitted within the block.

    Raises:
        ValueError: If an unrecognized field name is passed — context keys are a
            fixed, audited set, so a typo fails fast rather than logging silently.

    The fields are reset to their prior values on exit (including on exception), so
    context never leaks past the block. Nested binds override and then restore.
    """
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown log context field(s): {', '.join(sorted(unknown))}")
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = [
        (_CONTEXT[name], _CONTEXT[name].set(value)) for name, value in fields.items()
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class ContextFilter(logging.Filter):
    """Stamp the active :func:`bind_context` fields onto each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        bound = {name: var.get() for name, var in _CONTEXT.items()}
        setattr(record, _CTX_RECORD_ATTR, {k: v for k, v in bound.items() if v is not None})
        return True


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object over a fixed field schema."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, _CTX_RECORD_ATTR, {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, default=str)


class _KdiveHandler(logging.StreamHandler):
    """Marker handler so :func:`configure_logging` can stay idempotent."""


def configure_logging(level: str = "INFO", *, secret_registry: SecretRegistry) -> None:
    """Install the JSON formatter + context filter on the root logger, idempotently.

    Safe to call from each entrypoint (server, worker, reconciler): a second call
    updates the level and redaction registry but does not add a duplicate handler.

    Args:
        level: A standard logging level name (e.g. ``"INFO"``, ``"DEBUG"``); an
            unknown name falls back to ``INFO``.
        secret_registry: Registry used by the logging redaction filter.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in root.handlers:
        if isinstance(handler, _KdiveHandler):
            _set_redaction_filter(handler, secret_registry)
            return
    handler = _KdiveHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    _set_redaction_filter(handler, secret_registry)
    root.addHandler(handler)


def _set_redaction_filter(handler: logging.Handler, registry: SecretRegistry) -> None:
    handler.filters = [
        existing for existing in handler.filters if not isinstance(existing, SecretRedactionFilter)
    ]
    handler.addFilter(SecretRedactionFilter(registry))
