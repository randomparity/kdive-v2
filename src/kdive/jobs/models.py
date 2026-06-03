"""Job-handler type and registry for the durable queue (ADR-0018, issue #9).

A :data:`JobHandler` is the async callable a worker invokes for one claimed
:class:`~kdive.domain.models.Job`; it runs the op and returns a ``result_ref``
(object-store key) or ``None``, or raises to fail the job. :class:`HandlerRegistry`
binds exactly one handler per :class:`~kdive.domain.models.JobKind`; the plane issues
(#11+) populate it at worker startup and the worker dispatches by ``Job.kind``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg import AsyncConnection

from kdive.domain.models import Job, JobKind

type JobHandler = Callable[[AsyncConnection, Job], Awaitable[str | None]]


class DuplicateHandler(RuntimeError):
    """A second handler was registered for a kind that already has one."""


class HandlerRegistry:
    """A one-handler-per-kind registry the worker dispatches through."""

    def __init__(self) -> None:
        self._handlers: dict[JobKind, JobHandler] = {}

    def register(self, kind: JobKind, handler: JobHandler) -> None:
        """Bind ``handler`` to ``kind``.

        Raises:
            DuplicateHandler: A handler is already registered for ``kind`` — two
                issues must not silently both claim a kind.
        """
        if kind in self._handlers:
            raise DuplicateHandler(f"a handler is already registered for {kind}")
        self._handlers[kind] = handler

    def get(self, kind: JobKind) -> JobHandler | None:
        """Return the handler for ``kind``, or ``None`` if none is registered."""
        return self._handlers.get(kind)
