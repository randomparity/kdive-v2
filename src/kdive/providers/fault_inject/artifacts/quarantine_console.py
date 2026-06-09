"""The fault-inject object-store quarantine loop (ADR-0075).

A fault-inject-only seam — the generic provider ports stay unchanged. It models a write that
lands **before** secret registration completes: it reads a high-entropy ``secret_ref`` **raw**
(unregistered), emits it into a synthetic console transcript, and persists that transcript raw
and flagged ``QUARANTINED``. It then resolves the same ref through an injected ``SecretBackend``
(which registers the value before returning it), re-fetches the quarantined object from the
store, redacts it with a ``Redactor`` over the same registry, and persists a ``REDACTED``
sibling — healing the quarantine. The per-op scope is released only **after** the heal persist,
so the value is registered at the moment the heal masks it and never lingers past the op. The
quarantined raw object is retained for provenance; the redacted-only serve gates keep it
unservable.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    FetchedArtifact,
    StoredArtifact,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import (
    SecretBackend,
    read_secret_file,
    secret_backend_from_env,
    secrets_root_from_env,
)

_TENANT = "fault-inject"
_RETENTION_CLASS = "console"
_QUARANTINED_NAME = "console-quarantined"
_HEALED_NAME = "console-quarantined-redacted"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


class QuarantineHealOutput(NamedTuple):
    """The retained quarantined raw, the healed redacted sibling, and the masked snippet."""

    quarantined: StoredArtifact
    healed: StoredArtifact
    transcript_snippet: str


def _quarantined_transcript(value: str) -> str:
    """Return a console transcript that echoes the credential **bare**, as a real console would.

    Emitting it bare (not ``password=<value>``) keeps the heal's mask a real test of the
    register->exact-value path (ADR-0075), not a coincidence of the Redactor's key=value regex.
    """
    return (
        "fault-inject console boot\n"
        f"[bmc] handshake echoed credential {value} to the console\n"
        "fault-inject console ready\n"
    )


class FaultInjectQuarantineConsole:
    """Store raw + quarantined, resolve, re-fetch, heal to a redacted sibling, release last."""

    def __init__(
        self,
        *,
        backend: SecretBackend,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        secrets_root: Path,
        scope: object,
    ) -> None:
        """Build the loop.

        Args:
            backend: The secret backend, bound to ``registry`` under ``scope`` so the value it
                resolves is registered under the same scope this loop releases.
            registry: The registry the backend registers into and the ``Redactor`` masks from.
            store_factory: Builds the object store the transcripts are persisted to / fetched from.
            secret_ref: The absolute path of the secret under the allowlisted secrets root.
            secrets_root: The allowlisted root the unregistered pre-write read is confined to.
            scope: The per-op-unique registry scope identity, single-sourced here — the backend
                registers under it and the loop releases it, so they cannot diverge.
        """
        self._backend = backend
        self._registry = registry
        self._store_factory = store_factory
        self._secret_ref = secret_ref
        self._secrets_root = secrets_root
        self._scope = scope

    @classmethod
    def for_op(
        cls,
        *,
        registry: SecretRegistry,
        store_factory: Callable[[], _StorePort],
        secret_ref: str,
        scope: object,
    ) -> FaultInjectQuarantineConsole:
        """Build the loop with a ``FileRefBackend`` + root from ``KDIVE_SECRETS_ROOT``."""
        return cls(
            backend=secret_backend_from_env(registry=registry, scope=scope),
            registry=registry,
            store_factory=store_factory,
            secret_ref=secret_ref,
            secrets_root=secrets_root_from_env(),
            scope=scope,
        )

    def emit_and_persist(self, *, system_id: UUID) -> QuarantineHealOutput:
        """Run the quarantine loop; release the op's scope only after the heal persist.

        Reads the secret raw (unregistered), persists it raw + ``QUARANTINED`` (the
        pre-registration write), resolves the ref (registering the value under the op scope),
        re-fetches the quarantined object, redacts it, persists a ``REDACTED`` sibling, and
        releases the scope **only after** that persist — so the value is registered when the
        heal masks it and is evicted afterward; a failed heal still releases the scope.

        Args:
            system_id: The System the synthetic console belongs to (the artifact owner).

        Returns:
            The retained quarantined raw ``StoredArtifact``, the healed redacted sibling, and the
            redacted transcript snippet a caller would surface.
        """
        store = self._store_factory()
        try:
            raw_value = read_secret_file(self._secrets_root, self._secret_ref)
            quarantined = store.put_artifact(
                self._write_request(
                    system_id,
                    name=_QUARANTINED_NAME,
                    data=_quarantined_transcript(raw_value).encode("utf-8"),
                    sensitivity=Sensitivity.QUARANTINED,
                )
            )
            self._backend.resolve(self._secret_ref)
            fetched = store.get_artifact(quarantined.key, quarantined.etag)
            redacted = Redactor(registry=self._registry).redact_text(fetched.data.decode("utf-8"))
            healed = store.put_artifact(
                self._write_request(
                    system_id,
                    name=_HEALED_NAME,
                    data=redacted.encode("utf-8"),
                    sensitivity=Sensitivity.REDACTED,
                )
            )
            return QuarantineHealOutput(
                quarantined=quarantined, healed=healed, transcript_snippet=redacted
            )
        finally:
            self._registry.release(self._scope)

    @staticmethod
    def _write_request(
        system_id: UUID, *, name: str, data: bytes, sensitivity: Sensitivity
    ) -> ArtifactWriteRequest:
        return ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind="systems",
            owner_id=str(system_id),
            name=name,
            data=data,
            sensitivity=sensitivity,
            retention_class=_RETENTION_CLASS,
        )
