"""Value/pattern redaction for the return/persistence and logging paths (ADR-0027).

Ported from the PoC ``kdive.safety.redaction``. Runtime code should pass the
app-owned ``SecretRegistry`` so ``Redactor`` and ``SecretRedactionFilter`` share
the same source of registered values. ``PROCESS_SECRET_REGISTRY`` remains the
default for tests and small CLI helpers.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry

REDACTION = "[REDACTED]"

_USERINFO_AUTHORITY = re.compile(r"[^/@\s]+:[^/@\s]*@")


def redact_url_credentials(url: str) -> str:
    """Return ``url`` with any basic-auth ``userinfo`` removed; total and credential-safe.

    Strips the ``user:pass@`` authority for both scheme-ful URLs (via ``urlsplit``) and
    schemeless/relative inputs (via a regex fallback, since ``urlsplit`` only exposes
    credentials when a ``//netloc`` is present). Never raises: a value that cannot be
    parsed is redacted to ``REDACTION`` rather than echoed.
    """
    try:
        parts = urlsplit(url)
        if parts.netloc:
            if parts.username or parts.password:
                host = parts.hostname or ""
                if parts.port:
                    host = f"{host}:{parts.port}"
                url = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
            return url
        return _USERINFO_AUTHORITY.sub("", url)
    except (ValueError, UnicodeError):
        return REDACTION


class Redactor:
    """Redacts known secret values and ``key=value`` patterns from text and structures.

    Seeds from ``registry`` in addition to any explicit ``secret_values``. The snapshot
    is taken at construction; build a fresh ``Redactor`` per response so newly
    registered values are reflected (ADR-0027 §3).
    """

    def __init__(
        self,
        secret_values: list[str] | None = None,
        *,
        registry: SecretRegistry | None = None,
    ) -> None:
        registry = PROCESS_SECRET_REGISTRY if registry is None else registry
        merged = [*registry.snapshot(), *(secret_values or [])]
        self._secret_values = [value for value in merged if value]
        secret_name = r"[A-Za-z0-9_-]*(?:password|passwd|token|api[_-]?key|secret)[A-Za-z0-9_-]*"
        self._key_value_pattern = re.compile(rf"(?i)\b({secret_name})(\s*[=:]\s*)([^\s]+)")
        self._secret_key_pattern = re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret)")

    def redact_text(self, text: str) -> str:
        redacted = text
        for value in self._secret_values:
            redacted = redacted.replace(value, REDACTION)
        return self._key_value_pattern.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}", redacted
        )

    def redact_mapping(self, values: Mapping[str, object]) -> dict[str, object]:
        return self.redact_value(dict(values))

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            sensitive = value.get("sensitive") is True
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if (sensitive and key == "path") or self._secret_key_pattern.search(str(key)):
                    redacted[key] = REDACTION
                else:
                    redacted[key] = self.redact_value(item)
            return redacted
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        return value


class SecretRedactionFilter(logging.Filter):
    """Handler-boundary redaction for the logging path.

    Masks the fully rendered message AND any exception/stack text against the registry
    snapshot plus the ``Redactor`` key/value patterns. Caches a ``Redactor``, rebuilding
    only when the registry version changes.
    """

    def __init__(self, registry: SecretRegistry) -> None:
        super().__init__()
        self._registry = registry
        self._cached_version = -1
        self._redactor = Redactor(registry=registry)

    def _current(self) -> Redactor:
        version = self._registry.version()
        if version != self._cached_version:
            self._redactor = Redactor(registry=self._registry)
            self._cached_version = version
        return self._redactor

    def filter(self, record: logging.LogRecord) -> bool:
        redactor = self._current()
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — bad %-formatting must never break logging
            message = f"{record.msg!r} args={record.args!r}"
        if not isinstance(message, str):
            message = str(message)
        record.msg = redactor.redact_text(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redactor.redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redactor.redact_text(record.stack_info)
        return True
