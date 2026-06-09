"""The fault-inject quarantine loop: store-raw -> resolve -> heal -> release (ADR-0075)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kdive.domain.models import Sensitivity
from kdive.providers.fault_inject.artifacts.quarantine_console import (
    FaultInjectQuarantineConsole,
    QuarantineHealOutput,
    _quarantined_transcript,
)
from kdive.security.secrets.redaction import REDACTION, Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from kdive.store.objectstore import ArtifactWriteRequest, FetchedArtifact, StoredArtifact

_SENTINEL = "quar-1a2b3c4d5e6f70819273645566778899aabbccddeeff0011"  # high-entropy, unique


class _SpyStore:
    """In-memory put/get store; records each put and the registry snapshot at write time."""

    def __init__(self, registry: SecretRegistry) -> None:
        self._registry = registry
        self.requests: list[ArtifactWriteRequest] = []
        self.snapshot_at_put: list[frozenset[str]] = []
        self.objects: dict[str, ArtifactWriteRequest] = {}

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.requests.append(request)
        self.snapshot_at_put.append(self._registry.snapshot())
        self.objects[request.key()] = request
        return StoredArtifact(
            key=request.key(),
            etag="spy-etag",
            sensitivity=request.sensitivity,
            retention_class=request.retention_class,
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        request = self.objects[key]
        return FetchedArtifact(request.data, request.sensitivity, request.retention_class)


def _sentinel_ref(root: Path) -> str:
    secret = root / "fault-inject" / "quarantine-sentinel"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(_SENTINEL, encoding="utf-8")
    return str(secret)


def _console(
    root: Path, registry: SecretRegistry, store: _SpyStore, scope: object, ref: str
) -> FaultInjectQuarantineConsole:
    return FaultInjectQuarantineConsole(
        backend=FileRefBackend(root, registry, scope=scope),
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        secrets_root=root,
        scope=scope,
    )


def test_quarantined_raw_retained_and_sibling_is_healed(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    output = _console(tmp_path, registry, store, scope, ref).emit_and_persist(system_id=uuid4())

    assert isinstance(output, QuarantineHealOutput)
    # The pre-registration write is raw and flagged quarantined.
    quar = output.quarantined
    assert quar.sensitivity is Sensitivity.QUARANTINED
    quar_bytes = store.objects[quar.key].data.decode("utf-8")
    assert _SENTINEL in quar_bytes  # raw: the quarantined object still contains the secret
    # The healed sibling is redacted and masked.
    healed = output.healed
    assert healed.sensitivity is Sensitivity.REDACTED
    assert healed.key != quar.key
    healed_bytes = store.objects[healed.key].data.decode("utf-8")
    assert _SENTINEL not in healed_bytes
    assert REDACTION in healed_bytes
    # The returned snippet (a concrete field, not the StoredArtifact tuple) is masked too.
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet


def test_masking_is_the_registry_exact_value_path_not_the_pattern(tmp_path: Path) -> None:
    control = Redactor(registry=SecretRegistry())  # empty registry, no value seeded
    assert _SENTINEL in control.redact_text(_quarantined_transcript(_SENTINEL))

    seeded = SecretRegistry()
    seeded.register(_SENTINEL, scope="probe")
    assert _SENTINEL not in Redactor(registry=seeded).redact_text(
        _quarantined_transcript(_SENTINEL)
    )


def test_value_registered_at_heal_write_and_gone_after(tmp_path: Path) -> None:
    registry = SecretRegistry()
    store = _SpyStore(registry)
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    _console(tmp_path, registry, store, scope, ref).emit_and_persist(system_id=uuid4())

    # Two puts: [0] the quarantined pre-registration write (value NOT yet registered),
    # [1] the healed sibling (value registered, so the Redactor masked it).
    assert _SENTINEL not in store.snapshot_at_put[0]
    assert _SENTINEL in store.snapshot_at_put[1]
    # Released after persist: gone from the registry once the loop returns.
    assert _SENTINEL not in registry.snapshot()


def test_scope_released_even_when_heal_persist_raises(tmp_path: Path) -> None:
    registry = SecretRegistry()
    scope = f"op-{uuid4()}"
    ref = _sentinel_ref(tmp_path)

    class _FailOnHealStore:
        def __init__(self) -> None:
            self._puts = 0
            self.objects: dict[str, ArtifactWriteRequest] = {}

        def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
            self._puts += 1
            if self._puts == 2:  # fail the heal write, not the quarantine write
                raise RuntimeError("object store down")
            self.objects[request.key()] = request
            return StoredArtifact(request.key(), "e", request.sensitivity, request.retention_class)

        def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
            request = self.objects[key]
            return FetchedArtifact(request.data, request.sensitivity, request.retention_class)

    console = FaultInjectQuarantineConsole(
        backend=FileRefBackend(tmp_path, registry, scope=scope),
        registry=registry,
        store_factory=_FailOnHealStore,
        secret_ref=ref,
        secrets_root=tmp_path,
        scope=scope,
    )

    with pytest.raises(RuntimeError):
        console.emit_and_persist(system_id=uuid4())
    assert _SENTINEL not in registry.snapshot()  # finally-release evicts despite the failure


def test_concurrent_op_release_does_not_evict_this_ops_value(tmp_path: Path) -> None:
    # Distinct sentinels under distinct scopes prove isolation by scope, not shared-value
    # refcounting (ADR-0073 implementation binding, carried to the quarantine heal).
    registry = SecretRegistry()
    value_a = "quar-a-44ffe1c7b2a8053f1e0c93a7d55be21c40f8b6e9"
    value_b = "quar-b-77aac0e5d1f7b9264a0e1c83975dd4be20c41f9a7"
    (tmp_path / "fault-inject").mkdir(parents=True, exist_ok=True)
    file_a = tmp_path / "fault-inject" / "a"
    file_b = tmp_path / "fault-inject" / "b"
    file_a.write_text(value_a, encoding="utf-8")
    file_b.write_text(value_b, encoding="utf-8")
    scope_a, scope_b = "op-a", "op-b"

    FileRefBackend(tmp_path, registry, scope=scope_a).resolve(str(file_a))
    FileRefBackend(tmp_path, registry, scope=scope_b).resolve(str(file_b))
    assert {value_a, value_b} <= registry.snapshot()

    registry.release(scope_b)  # op B heals + releases first
    assert value_a in registry.snapshot()  # op A's value survives for op A's own heal
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

    console = FaultInjectQuarantineConsole.for_op(
        registry=registry,
        store_factory=lambda: store,
        secret_ref=ref,
        scope=scope,
    )
    output = console.emit_and_persist(system_id=uuid4())

    assert output.healed.sensitivity is Sensitivity.REDACTED
    assert _SENTINEL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet
    assert _SENTINEL not in registry.snapshot()
