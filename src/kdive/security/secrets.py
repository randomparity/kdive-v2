"""By-reference secret backend (ADR-0027 §5-6, refines ADR-0012).

``SecretBackend`` is the pluggable interface; a manager backend (Vault, a cloud
secret manager) drops in later behind it with no call-site change. M0 ships
``FileRefBackend``: it resolves a file reference only within an allowlisted root and
registers the resolved value into the redaction registry **before** returning it, so
the register-before-return ordering is a structural invariant — there is no return
path that yields the value without first registering it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from kdive.security.paths import PathSafetyError, confine_to_root
from kdive.security.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry


class SecretBackend(Protocol):
    """Resolve an opaque reference to a secret value, by reference only."""

    def resolve(self, ref: str) -> str: ...


class FileRefBackend:
    """Resolve a file reference to its contents, confined to an allowlisted root.

    The value is registered into ``registry`` (defaulting to the process-global
    ``PROCESS_SECRET_REGISTRY``) before it is returned, so any consumer that builds a
    ``Redactor`` next will mask it. A reference escaping ``root`` raises
    ``PathSafetyError`` before any file is read.
    """

    def __init__(
        self,
        root: Path,
        registry: SecretRegistry | None = None,
        *,
        scope: object | None = None,
    ) -> None:
        self._root = root
        self._registry = registry if registry is not None else PROCESS_SECRET_REGISTRY
        self._scope = scope

    def resolve(self, ref: str) -> str:
        resolved = confine_to_root(Path(ref), allowed_root=self._root)
        if not resolved.is_file():
            raise PathSafetyError("secret file does not exist")
        value = resolved.read_text(encoding="utf-8")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        self._registry.register(value, scope=self._scope)
        return value
