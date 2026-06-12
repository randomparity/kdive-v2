"""The in-target object-store artifact channel for remote-libvirt (ADR-0078).

The load-bearing in-target install/retrieve seam every later provider (M3 cloud, M4 bare
metal, M5 PowerVM) reuses. The worker mints a bounded, single-object **presigned URL**
(GET to pull a built kernel, PUT to push a vmcore), and this channel:

1. **registers the minted URL in the redaction registry before** the guest-agent exec — a
   presigned URL is a *bearer capability*, so an unregistered URL captured into a persisted
   transcript would be a live read/write grant until expiry;
2. runs a worker-composed, **constrained/allowlisted** in-guest command that carries the
   URL (the program allowlist is enforced by :class:`GuestAgentExec`, never by an in-guest
   shell);
3. redacts the captured transcript with a ``Redactor`` over the same registry — the URL is
   masked by exact value — and persists the redacted bytes;
4. releases the per-op registry scope **only after** the persist (in a ``finally``), so the
   capability never reaches the object store or a returned snippet unmasked and never
   lingers in the registry past the op.

The TLS client cert/key is consumed by the libvirt transport layer (ADR-0077) and never
reaches this seam, so it cannot appear in a transcript; this channel proves the
*transcript exact-value redaction* half of the M2 secret contract.

The raw :class:`AgentExecResult` is returned for the worker's in-process logic (exit
status, output) and must not be persisted or logged outside the redaction path — only the
redacted ``transcript_snippet`` and the persisted ``StoredArtifact`` are masked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple, Protocol

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import ArtifactWriteRequest, StoredArtifact
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_TENANT = "remote-libvirt"
_RETENTION_CLASS = "console"
_TRANSCRIPT_NAME = "in-target-exec-redacted"


class _StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _AgentExec(Protocol):
    def run(self, domain: Any, argv: list[str]) -> AgentExecResult: ...


class SeamOutput(NamedTuple):
    """The raw exec result, the persisted redacted transcript, and a masked snippet.

    ``result`` carries the in-guest command's exit status and raw output for the worker's
    own logic; it is **not** redacted and must not be persisted or logged outside the
    redaction path. ``artifact`` and ``transcript_snippet`` are masked.
    """

    result: AgentExecResult
    artifact: StoredArtifact
    transcript_snippet: str


def _build_transcript(argv: list[str], result: AgentExecResult) -> str:
    """Render the command line + captured streams as a console-style transcript.

    The command line echoes the full argv (which carries the capability URL); the URL is
    masked downstream by the registry-seeded ``Redactor``, never by chance of formatting.
    """
    lines = [
        "$ " + " ".join(argv),
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
        f"exit status: {result.exit_status}",
    ]
    return "\n".join(lines)


class InTargetArtifactChannel:
    """Register a minted presigned URL, run a constrained in-guest command, redact, persist.

    The per-op registry ``scope`` is **single-sourced** here: the caller mints the URL (via
    ``store.presign_get`` / ``presign_put``) and hands it over unused; this channel registers
    it under ``scope`` and releases that same ``scope`` after the persist, so registration and
    release cannot diverge and strand a live capability in the registry.
    """

    def __init__(
        self,
        *,
        registry: SecretRegistry,
        agent_exec: _AgentExec,
        store_factory: Callable[[], _StorePort],
        scope: object,
    ) -> None:
        self._registry = registry
        self._agent_exec = agent_exec
        self._store_factory = store_factory
        self._scope = scope

    def exec_with_capability(
        self,
        domain: Any,
        *,
        capability_url: str,
        argv: list[str],
        owner_kind: str,
        owner_id: str,
    ) -> SeamOutput:
        """Register ``capability_url``, run ``argv`` in-guest, redact-and-persist, release.

        ``argv`` is the worker-composed constrained command carrying ``capability_url``;
        its program is allowlisted by :class:`GuestAgentExec`. The URL is registered before
        the exec so the captured transcript is masked by exact value; the per-op scope is
        released only after the persist (and on every failure path).

        Args:
            domain: The libvirt domain handle the guest-agent command runs against.
            capability_url: The freshly minted, as-yet-unused presigned URL.
            argv: The constrained command (``argv[0]`` is an allowlisted program path).
            owner_kind: The artifact owner kind for the persisted transcript (e.g. ``systems``).
            owner_id: The artifact owner id.

        Returns:
            The raw :class:`AgentExecResult`, the persisted redacted ``StoredArtifact``, and
            the redacted transcript snippet.

        Raises:
            CategorizedError: propagated from :class:`GuestAgentExec` (an empty/non-allowlisted
                argv, an unreachable agent, a timeout, or a malformed reply) or the object
                store; the scope is released before the error escapes.
        """
        self._registry.register(capability_url, scope=self._scope)
        try:
            result = self._agent_exec.run(domain, argv)
            transcript = _build_transcript(argv, result)
            redacted = Redactor(registry=self._registry).redact_text(transcript)
            artifact = self._store_factory().put_artifact(
                ArtifactWriteRequest(
                    tenant=_TENANT,
                    owner_kind=owner_kind,
                    owner_id=owner_id,
                    name=_TRANSCRIPT_NAME,
                    data=redacted.encode("utf-8"),
                    sensitivity=Sensitivity.REDACTED,
                    retention_class=_RETENTION_CLASS,
                )
            )
            return SeamOutput(result=result, artifact=artifact, transcript_snippet=redacted)
        finally:
            self._registry.release(self._scope)
