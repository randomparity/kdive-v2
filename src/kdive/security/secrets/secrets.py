"""By-reference secret backend (ADR-0027 §5-6, refines ADR-0012).

``SecretBackend`` is the pluggable interface; a manager backend (Vault, a cloud
secret manager) drops in later behind it with no call-site change. M0 ships
``FileRefBackend``: it resolves a file reference only within an allowlisted root and
registers the resolved value into the redaction registry **before** returning it, so
the register-before-return ordering is a structural invariant — there is no return
path that yields the value without first registering it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from kdive.security.secrets.paths import PathSafetyError, confine_to_root
from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry

_SECRETS_ROOT_ENV = "KDIVE_SECRETS_ROOT"  # pragma: allowlist secret - env var name, not a value
_DEFAULT_SECRETS_ROOT = "/var/lib/kdive/secrets"

_MAX_SECRET_FILE_BYTES = 64 * 1024
"""A secret (token, password, SSH key) is small. A larger file under the secrets
root is a mis-pointed reference, not a credential — read-capping it keeps an
operator error from turning a multi-megabyte value into a redaction needle that is
``str.replace``-scanned across every response."""


def read_secret_file(root: Path, ref: str) -> str:
    """Read a secret file's value, confined to ``root``, size-capped, newline-stripped.

    Does **not** register the value. :meth:`FileRefBackend.resolve` registers after this read;
    callers that need the raw value without registration (the ADR-0075 quarantine pre-write)
    call this directly.

    Raises:
        PathSafetyError: ``ref`` escapes ``root``, does not exist, or exceeds the cap.
    """
    resolved = confine_to_root(Path(ref), allowed_root=root)
    if not resolved.is_file():
        raise PathSafetyError("secret file does not exist")
    if resolved.stat().st_size > _MAX_SECRET_FILE_BYTES:
        raise PathSafetyError("secret file exceeds the maximum secret size")
    value = resolved.read_text(encoding="utf-8")
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def secrets_root_from_env() -> Path:
    """Return the allowlisted secrets root from ``KDIVE_SECRETS_ROOT`` (or the default)."""
    return Path(os.environ.get(_SECRETS_ROOT_ENV, _DEFAULT_SECRETS_ROOT))


class SecretBackend(Protocol):
    """Resolve an opaque reference to a secret value, by reference only."""

    def resolve(self, ref: str) -> str: ...


class FileRefBackend:
    """Resolve a file reference to its contents, confined to an allowlisted root.

    The value is registered into ``registry`` before it is returned, so any consumer
    that builds a ``Redactor`` from that same registry next will mask it. If no
    registry is supplied, the process-global default is used for tests and small CLI
    helpers. A reference escaping ``root`` raises
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
        value = read_secret_file(self._root, ref)
        self._registry.register(value, scope=self._scope)
        return value


def secret_backend_from_env(
    *, registry: SecretRegistry | None = None, scope: object | None = None
) -> FileRefBackend:
    """Build the file-ref secret backend from ``KDIVE_SECRETS_ROOT``.

    Resolves credentials only within the allowlisted secrets root and registers each resolved
    value into ``registry`` under ``scope``. Passing ``scope=None`` keeps the original
    process-lifetime scope. Opens no file at construction — the root is read on the first
    ``resolve``.
    """
    return FileRefBackend(secrets_root_from_env(), registry=registry, scope=scope)
