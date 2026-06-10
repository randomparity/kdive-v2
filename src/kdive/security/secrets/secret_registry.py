"""Registry of known secret values (ADR-0027, refines ADR-0012).

Ported from the PoC ``kdive.safety.secret_registry``. Both the logging
``SecretRedactionFilter`` and app-owned ``Redactor`` instances seed from the same
``SecretRegistry``, so a value resolved through a ``SecretBackend`` is masked on
the logging path and the return/persistence path without each call site
re-supplying it. Production composition passes an app-owned registry explicitly;
``PROCESS_SECRET_REGISTRY`` remains only for tests and small CLI helpers that do
not have an app composition owner.
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

    def scope_refs(self) -> frozenset[str]:
        """Return a presence projection of the scopes secrets are registered under — no values.

        Powers ``secrets.list``: it reports *that* secrets exist, bucketed by the scope they
        were registered against, without ever exposing a value. Scope keys vary by call site:
        a ``str`` ref (e.g. ``"ref://..."``) is surfaced verbatim; the process-global scope
        (``scope=None`` registrations, ADR-0012 lifetime values) reports as
        ``"<process-global>"``; any other (non-string) scope owner — e.g. a bare ``object()``
        sentinel — reports as ``"<scoped>"`` so no object ``repr`` (which would expose a heap
        address) reaches operator output.
        """
        with self._lock:
            keys = list(self._by_scope)
        return frozenset(self._scope_label(key) for key in keys)

    def _scope_label(self, key: object) -> str:
        """Map one ``_by_scope`` key to a value-free presence label (never an object repr)."""
        if key is self._GLOBAL or key == self._GLOBAL:
            return "<process-global>"
        if isinstance(key, str):
            return key
        return "<scoped>"

    def version(self) -> int:
        with self._lock:
            return self._version

    def clear(self) -> None:
        """Drop all registered values and advance the version for cached redactors."""
        with self._lock:
            self._refcount.clear()
            self._by_scope.clear()
            self._version += 1


PROCESS_SECRET_REGISTRY = SecretRegistry()
"""Process registry for tests and CLI helpers without an app-owned registry."""
