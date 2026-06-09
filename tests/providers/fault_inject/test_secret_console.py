"""The fault-inject secret-console loop: resolve->emit->redact->persist->release (ADR-0073)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.models import Sensitivity
from kdive.providers.fault_inject.artifacts.secret_console import (
    FaultInjectSecretConsole,
    SecretConsoleOutput,
    _synthetic_transcript,
)
from kdive.security.secrets.redaction import REDACTION, Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from kdive.store.objectstore import ArtifactWriteRequest, StoredArtifact

_SENTINEL = "bmc-7f3a9c1e5d2b8a04f6e1c0937a55de28b41f9c6d0e2a7b3"  # high-entropy, unique


class _SpyStore:
    """Records every persisted request's bytes and the registry snapshot at write time."""

    def __init__(self, registry: SecretRegistry) -> None:
        self._registry = registry
        self.requests: list[ArtifactWriteRequest] = []
        self.snapshot_at_write: frozenset[str] | None = None

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        self.snapshot_at_write = self._registry.snapshot()
        return StoredArtifact(
            key=request.key(),
            etag="spy-etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )


def _sentinel_ref(root: Path) -> str:
    """Write the sentinel under the root; return its ABSOLUTE ref (FileRefBackend needs it)."""
    secret = root / "fault-inject" / "console-sentinel"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(_SENTINEL, encoding="utf-8")
    return str(secret)


def _backend(root: Path, registry: SecretRegistry, scope: object) -> FileRefBackend:
    return FileRefBackend(root, registry, scope=scope)


def test_persisted_transcript_is_masked_and_carries_placeholder(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    system_id = uuid4()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)
    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )

    output = console.emit_and_persist(system_id=system_id)

    assert isinstance(output, SecretConsoleOutput)
    persisted = store.requests[-1].data.decode("utf-8")
    # mask-before-persist: the persisted bytes lack the raw sentinel and carry the placeholder.
    assert _SENTINEL not in persisted
    assert REDACTION in persisted
    # response snippet (a concrete field, not the StoredArtifact tuple) is masked too.
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert output.artifact.sensitivity is Sensitivity.REDACTED


def test_masking_is_the_registry_exact_value_path_not_the_pattern(tmp_path: Path) -> None:
    # Guard against a spurious pass: a control Redactor built from an EMPTY registry must
    # leave the bare sentinel PRESENT, proving the persisted masking came from the
    # register->exact-value path (ADR-0073), not the Redactor's independent key=value regex.
    control = Redactor(registry=SecretRegistry())  # empty registry, no value seeded
    assert _SENTINEL in control.redact_text(_synthetic_transcript(_SENTINEL))

    seeded = SecretRegistry()
    seeded.register(_SENTINEL, scope="probe")
    assert _SENTINEL not in Redactor(registry=seeded).redact_text(_synthetic_transcript(_SENTINEL))


def test_value_is_registered_at_persist_time_and_gone_after(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)
    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )

    console.emit_and_persist(system_id=uuid4())

    # Release-after-persist: the value was still registered at the moment of the write
    # (so the Redactor could mask it), and is evicted only after the loop returns.
    assert store.snapshot_at_write is not None
    assert _SENTINEL in store.snapshot_at_write
    assert _SENTINEL not in registry.snapshot()


def test_scope_is_released_even_when_persist_raises(tmp_path: Path) -> None:
    registry = SecretRegistry()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    class _FailingStore:
        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            raise RuntimeError("object store down")

    console = FaultInjectSecretConsole(
        backend=_backend(tmp_path, registry, scope),
        registry=registry,
        store_factory=lambda: _FailingStore(),
        secret_ref=ref,
        scope=scope,
    )

    with pytest.raises(RuntimeError):
        console.emit_and_persist(system_id=uuid4())
    # The finally-release still evicts the value so a failed persist does not leak a
    # global redaction needle that outlives the op.
    assert _SENTINEL not in registry.snapshot()


def test_concurrent_op_release_does_not_evict_this_ops_value(tmp_path: Path) -> None:
    # Two ops resolve DISTINCT high-entropy sentinels under DISTINCT scopes, so isolation is
    # proven by value-eviction (the registry refcounts by value), not masked by a shared value.
    registry = SecretRegistry()
    value_a = "op-a-9d4f1c7e2b6a8053f1e0c93a7d55be21c40f8b6e"
    value_b = "op-b-3a8c0e5d1f7b9264a0e1c83975dd4be20c41f9a7"
    (tmp_path / "fault-inject").mkdir(parents=True, exist_ok=True)
    file_a = tmp_path / "fault-inject" / "a"
    file_b = tmp_path / "fault-inject" / "b"
    file_a.write_text(value_a, encoding="utf-8")
    file_b.write_text(value_b, encoding="utf-8")
    scope_a, scope_b = "op-a", "op-b"

    FileRefBackend(tmp_path, registry, scope=scope_a).resolve(str(file_a))
    FileRefBackend(tmp_path, registry, scope=scope_b).resolve(str(file_b))
    assert {value_a, value_b} <= registry.snapshot()

    # Op B releases first; op A's value must survive (distinct scope, distinct value).
    registry.release(scope_b)
    assert value_a in registry.snapshot()
    assert value_b not in registry.snapshot()

    registry.release(scope_a)
    assert value_a not in registry.snapshot()


def test_for_op_binds_backend_to_the_op_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    ref = _sentinel_ref(tmp_path)
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = "op-xyz"

    console = FaultInjectSecretConsole.for_op(
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )
    output = console.emit_and_persist(system_id=uuid4())

    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert _SENTINEL not in registry.snapshot()
