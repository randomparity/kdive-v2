"""The fault-inject forced-secret-resolution loop (ADR-0073).

A fault-inject-only seam — the generic provider ports stay unchanged. It resolves a
high-entropy ``secret_ref`` through an injected ``SecretBackend`` (which registers the
value before returning it), emits the value into a synthetic console transcript, redacts
that transcript with a ``Redactor`` built from the same registry, persists the redacted
bytes, and releases the per-op scope **only after** the persist — so no resolved value
reaches the object store or the returned snippet unmasked, and none lingers in the
registry past the op.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_TENANT = "fault-inject"
_RETENTION_CLASS = "console"
_ARTIFACT_NAME = "console-transcript-redacted"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class SecretConsoleOutput(NamedTuple):
    """The persisted redacted artifact plus the redacted transcript snippet a caller surfaces."""

    artifact: StoredArtifact
    transcript_snippet: str


def _synthetic_transcript(value: str) -> str:
    """Return a console transcript that echoes the resolved credential, as a real console would.

    The value is emitted **bare** (not as ``password=<value>``) so that *only* the registry's
    exact-value masking — not the ``Redactor``'s independent ``key=value`` regex — can mask it.
    This keeps the mask-before-persist assertion a real test of the register->mask path
    (ADR-0073), not a coincidence of pattern matching.
    """
    return (
        "fault-inject console boot\n"
        f"[bmc] handshake echoed credential {value} to the console\n"
        "fault-inject console ready\n"
    )


class FaultInjectSecretConsole:
    """Resolve a secret, emit it into a transcript, redact-and-persist, release after persist."""

    def __init__(
        self,
        *,
        backend: SecretBackend,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        scope: object,
    ) -> None:
        """Build the loop.

        Args:
            backend: The secret backend, already bound to ``registry`` under ``scope`` so the
                value it resolves is registered under the same scope this loop releases.
            registry: The registry the backend registers into and the ``Redactor`` masks from.
            store_factory: Builds the object store the redacted transcript is persisted to.
            secret_ref: The absolute path of the secret under the allowlisted secrets root.
            scope: The per-op-unique registry scope identity. It is **single-sourced** here —
                the backend must register under this same scope and the loop releases it — so
                registration and release cannot diverge and leave the value un-evicted.
        """
        self._backend = backend
        self._registry = registry
        self._store_factory = store_factory
        self._secret_ref = secret_ref
        self._scope = scope

    @classmethod
    def for_op(
        cls,
        *,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        scope: object,
    ) -> FaultInjectSecretConsole:
        """Build the loop with a ``FileRefBackend`` bound to ``registry`` under ``scope``.

        The backend resolves under the allowlisted ``KDIVE_SECRETS_ROOT`` (ADR-0027) and
        registers each resolved value under the per-op ``scope`` the worker boundary owns —
        the same scope this loop releases after persist (single-sourced, so they cannot
        diverge).
        """
        backend = secret_backend_from_env(registry=registry, scope=scope)
        return cls(
            backend=backend,
            registry=registry,
            store_factory=store_factory,
            secret_ref=secret_ref,
            scope=scope,
        )

    def emit_and_persist(self, *, system_id: UUID) -> SecretConsoleOutput:
        """Run the full loop; release the op's scope only after the persist.

        Resolves the secret (registered under the op scope before return), emits it into a
        synthetic transcript, redacts that transcript with a ``Redactor`` over the same
        registry, persists the redacted bytes, and releases the scope **only after** the
        persist — never before, so no resolved value reaches the store or the returned
        snippet unmasked and none lingers in the registry past the op.

        Args:
            system_id: The System the synthetic console belongs to (the artifact owner).

        Returns:
            The persisted redacted ``StoredArtifact`` and the redacted transcript snippet.
        """
        try:
            value = self._backend.resolve(self._secret_ref)
            transcript = _synthetic_transcript(value)
            redactor = Redactor(registry=self._registry)
            redacted = redactor.redact_text(transcript)
            artifact = self._store_factory().put_artifact(
                ArtifactWriteRequest(
                    tenant=_TENANT,
                    owner_kind="systems",
                    owner_id=str(system_id),
                    name=_ARTIFACT_NAME,
                    data=redacted.encode("utf-8"),
                    sensitivity=Sensitivity.REDACTED,
                    retention_class=_RETENTION_CLASS,
                )
            )
            return SecretConsoleOutput(artifact=artifact, transcript_snippet=redacted)
        finally:
            self._registry.release(self._scope)
