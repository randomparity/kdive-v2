"""Tests for the remote console collector: streaming, rotation, redaction, assembly (ADR-0095)."""

from __future__ import annotations

from uuid import uuid4

from kdive.providers.remote_libvirt.console_collector import ConsoleCollector
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry

_SYSTEM = uuid4()


class FakeStream:
    """A console stream that yields queued chunks, then raises or ends per script."""

    def __init__(self, chunks: list[bytes], *, drop_after: int | None = None) -> None:
        self._chunks = list(chunks)
        self._drop_after = drop_after
        self._served = 0
        self.closed = False

    def recv(self, nbytes: int) -> bytes:
        if self._drop_after is not None and self._served >= self._drop_after:
            raise ConnectionResetError("stream dropped")
        if not self._chunks:
            return b""
        self._served += 1
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeOpenConsole:
    """An opener that hands out a scripted stream per (re)connect, recording opens."""

    def __init__(self, streams: list[FakeStream]) -> None:
        self._streams = list(streams)
        self.opens = 0

    def __call__(self, system_id):  # noqa: ANN001, ANN204 - duck-typed test seam
        self.opens += 1
        if self._streams:
            return self._streams.pop(0)
        return FakeStream([])


class FakePartStore:
    """An in-memory part store recording parts and the assembled artifact."""

    def __init__(self) -> None:
        self.parts: dict[int, bytes] = {}
        self.artifact: bytes | None = None

    def put_part(self, system_id, index: int, data: bytes) -> None:  # noqa: ANN001
        self.parts[index] = data

    def list_part_indices(self, system_id) -> list[int]:  # noqa: ANN001
        return sorted(self.parts)

    def read_part(self, system_id, index: int) -> bytes:  # noqa: ANN001
        return self.parts[index]

    def write_console_artifact(self, system_id, data: bytes) -> None:  # noqa: ANN001
        self.artifact = data

    def delete_part(self, system_id, index: int) -> None:  # noqa: ANN001
        self.parts.pop(index, None)


def _collector(open_console, store, *, registry=None, **kw) -> ConsoleCollector:  # noqa: ANN001
    return ConsoleCollector(
        _SYSTEM,
        open_console=open_console,
        store=store,
        secret_registry=registry or SecretRegistry(),
        **kw,
    )


def test_pump_buffers_decoded_output() -> None:
    stream = FakeStream([b"hello ", b"world\n"])
    store = FakePartStore()
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=1024)
    assert collector.pump_once() is True
    assert collector.pump_once() is True
    # Below threshold: nothing rotated yet, but finalize assembles the buffered bytes.
    assert store.parts == {}
    collector.finalize()
    assert store.artifact == b"hello world\n"


def test_rotation_on_threshold_uploads_numbered_parts() -> None:
    store = FakePartStore()
    stream = FakeStream([b"a" * 100, b"b" * 100])
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=100, seam_overlap=0)
    collector.pump_once()  # 100 bytes -> rotates part 0
    collector.pump_once()  # 100 bytes -> rotates part 1
    assert store.parts[0] == b"a" * 100
    assert store.parts[1] == b"b" * 100


def test_reconnect_on_stream_drop() -> None:
    dropping = FakeStream([b"first\n"], drop_after=1)
    fresh = FakeStream([b"second\n"])
    opener = FakeOpenConsole([dropping, fresh])
    store = FakePartStore()
    collector = _collector(opener, store, rotation_threshold=1024)
    assert collector.pump_once() is True  # reads "first"
    assert collector.pump_once() is False  # drop -> reconnect scheduled
    assert dropping.closed is True
    assert collector.pump_once() is True  # reads from the fresh stream
    assert opener.opens == 2
    collector.finalize()
    assert store.artifact == b"first\nsecond\n"


def test_crash_marker_forces_immediate_flush() -> None:
    store = FakePartStore()
    stream = FakeStream([b"boot ok\nKernel panic - not syncing\n"])
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=1_000_000)
    collector.pump_once()
    # Far below the size threshold, but the crash marker forced a rotation.
    assert store.parts, "crash marker should flush a part immediately"


def test_every_part_is_redacted_before_upload() -> None:
    registry = SecretRegistry()
    registry.register("hunter2", scope=None)
    store = FakePartStore()
    stream = FakeStream([b"login password=hunter2 done\n"])
    collector = _collector(
        FakeOpenConsole([stream]), store, registry=registry, rotation_threshold=1, seam_overlap=0
    )
    collector.pump_once()
    uploaded = b"".join(store.parts[i] for i in sorted(store.parts))
    assert b"hunter2" not in uploaded
    assert REDACTION.encode() in uploaded


def test_secret_straddling_the_rotation_seam_is_redacted() -> None:
    # The registered secret is split across two parts: its head ends part 0, its tail starts
    # part 1. Without the seam re-scan the literal value would survive in the joined artifact.
    secret = "supersecretvalue"  # pragma: allowlist secret - synthetic test redaction target
    registry = SecretRegistry()
    registry.register(secret, scope=None)
    store = FakePartStore()
    head, tail = secret[:8], secret[8:]
    # Part 0 ends exactly at the threshold mid-secret; part 1 carries the rest.
    chunk0 = b"prefix " + head.encode()
    chunk1 = tail.encode() + b" suffix\n"
    stream = FakeStream([chunk0, chunk1])
    collector = _collector(
        FakeOpenConsole([stream]),
        store,
        registry=registry,
        rotation_threshold=len(chunk0),
        seam_overlap=64,
    )
    collector.pump_once()  # rotates part 0 (head of the secret)
    collector.pump_once()  # part 1 (tail) -> seam re-scan must catch the join
    collector.finalize()
    assert store.artifact is not None
    assert secret.encode() not in store.artifact
    # No individual uploaded part may carry the raw secret head either (the holdback keeps the
    # straddling bytes out of every raw upload, not just the assembled artifact).
    for part in store.parts.values():
        assert head.encode() not in part or REDACTION.encode() in part


def test_seam_holdback_across_two_real_parts_redacts_and_does_not_duplicate() -> None:
    # Force two genuinely-uploaded parts with the secret split across the seam: part 0 fills
    # well past the overlap, the secret's head lands in part 0's held-back tail, the secret's
    # tail starts part 1. The raw head must not appear in part 0, and the carried bytes must
    # appear exactly once in the assembled artifact (no duplication).
    secret = "abcdefghijklmnop"  # pragma: allowlist secret - synthetic test redaction target
    registry = SecretRegistry()
    registry.register(secret, scope=None)
    store = FakePartStore()
    filler = b"x" * 200
    chunk0 = filler + secret[:8].encode()  # 208 bytes, > overlap
    chunk1 = secret[8:].encode() + b" tail\n"
    stream = FakeStream([chunk0, chunk1])
    collector = _collector(
        FakeOpenConsole([stream]),
        store,
        registry=registry,
        rotation_threshold=len(chunk0),
        seam_overlap=16,
    )
    collector.pump_once()  # uploads part 0 = filler[:-16] redacted, holds 16 raw bytes
    collector.pump_once()
    collector.finalize()
    assert store.artifact is not None
    assert secret.encode() not in store.artifact
    for part in store.parts.values():
        assert secret[:8].encode() not in part
    # filler appears once (no seam duplication): count the x-run length in the artifact.
    assert store.artifact.count(b"x") == 200


def test_finalize_assembles_ordered_parts_into_one_artifact() -> None:
    store = FakePartStore()
    stream = FakeStream([b"one ", b"two ", b"three"])
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=4, seam_overlap=0)
    while collector.pump_once():
        pass
    collector.finalize()
    assert store.artifact == b"one two three"


def test_finalize_is_idempotent() -> None:
    store = FakePartStore()
    stream = FakeStream([b"data\n"])
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=1024)
    collector.pump_once()
    collector.finalize()
    first = store.artifact
    collector.finalize()
    assert store.artifact == first
    assert collector.finalized is True


def test_restart_resumes_past_existing_parts() -> None:
    # A restart after a dead stream must not overwrite already-uploaded parts: a fresh
    # collector over the same store resumes numbering past the highest existing index.
    store = FakePartStore()
    store.parts = {0: b"old0", 1: b"old1"}
    stream = FakeStream([b"new\n"])
    collector = _collector(FakeOpenConsole([stream]), store, rotation_threshold=1, seam_overlap=0)
    collector.pump_once()
    assert 2 in store.parts
    assert store.parts[0] == b"old0"
    assert store.parts[1] == b"old1"


def test_empty_console_bytes_finalize_to_empty_artifact() -> None:
    store = FakePartStore()
    stream = FakeStream([])  # immediate EOF
    collector = _collector(FakeOpenConsole([stream]), store)
    assert collector.pump_once() is False
    collector.finalize()
    assert store.artifact == b""
