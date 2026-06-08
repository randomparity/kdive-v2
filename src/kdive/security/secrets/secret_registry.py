"""Process-scoped registry of known secret values (ADR-0027, refines ADR-0012).

Ported from the PoC ``kdive.safety.secret_registry``. Both the logging
``SecretRedactionFilter`` and every ``Redactor`` seed from
``PROCESS_SECRET_REGISTRY``, so a value resolved through a ``SecretBackend`` is
masked on the logging path and the return/persistence path without each call site
re-supplying it. This module is a leaf (no internal imports) to keep ``Redactor``
free of an import cycle through ``logging``.
"""

from __future__ import annotations

import threading


class SecretRegistry:
    """Thread-safe, reference-counted registry of known secret values.

    Values are reference-counted per ``scope`` so a caller that opts into a bounded
    scope (and calls ``release`` on teardown) keeps only credentials for its live
    owners. ``scope=None`` registers process-globally and is never evicted by
    ``release`` — per ADR-0012 those values are retained for the process lifetime.

    Empty/``None`` values are never stored (an empty credential would force-mask
    every string).
    """

    _GLOBAL = "__global__"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refcount: dict[str, int] = {}
        self._by_scope: dict[object, list[str]] = {}
        self._version = 0

    def register(self, value: str | None, *, scope: object | None) -> None:
        if not value:
            return
        key: object = self._GLOBAL if scope is None else scope
        with self._lock:
            self._by_scope.setdefault(key, []).append(value)
            self._refcount[value] = self._refcount.get(value, 0) + 1
            self._version += 1

    def release(self, scope: object | None) -> None:
        if scope is None:
            return  # the global scope is never evicted
        with self._lock:
            values = self._by_scope.pop(scope, [])
            if not values:
                return
            for value in values:
                remaining = self._refcount.get(value, 0) - 1
                if remaining <= 0:
                    self._refcount.pop(value, None)
                else:
                    self._refcount[value] = remaining
            self._version += 1

    def snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._refcount)

    def version(self) -> int:
        with self._lock:
            return self._version


PROCESS_SECRET_REGISTRY = SecretRegistry()
"""The one process-global registry both the logging filter and every ``Redactor``
seed from, so a resolved secret is masked on the logging and return/persistence
paths without each call site re-supplying it."""
