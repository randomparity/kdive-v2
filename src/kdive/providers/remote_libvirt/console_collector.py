"""Per-System remote console streamer + rotation/redaction + part assembly (ADR-0095).

`virDomainOpenConsole` delivers only output produced **after** the stream opens, with no
replayable backing log, so console parity (boot → crash) needs a long-lived owner that
opens the stream promptly and tees continuously. That owner is the reconciler-leader
(`console_hosting`); this module is the per-System streamer it hosts.

A :class:`ConsoleCollector` reads decoded console bytes into a bounded buffer and, on a size
threshold, **rotates** — uploading a numbered, **redacted** part object (S3 has no append).
Every part is redacted before upload. To catch a secret straddling the rotation seam the
collector **holds back** a trailing overlap of raw bytes from each rotation and prepends them
to the next part, so a secret split across the size threshold is redacted as one contiguous
run and is **never** uploaded raw in either part (the held-back bytes are uploaded only once,
redacted, with the next part). A crash marker in the stream forces an immediate flush so the
panic tail is the least-lost part. Finalization (on capture or teardown) flushes the held-back
tail and assembles the ordered parts into **one** concatenated console artifact in the shape
`classify_console` / `read_console_log` expect — kdive-side, not S3 multipart (the parts are
intentionally small).

Every host/store seam is injected so the collector is unit-testable without a libvirt host.
The console bytes are untrusted guest output: redaction runs before **any** upload or
assembled-artifact write, never after.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol
from uuid import UUID

from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# Steady-state rotation threshold (ADR-0095): small, so the unflushed crash-tail window is
# bounded — the durability trade the ADR's Consequences section calls out.
DEFAULT_ROTATION_THRESHOLD = 64 * 1024
# Trailing overlap re-scanned across the rotation seam so a secret split between two parts is
# still redacted (AC2). Sized to cover a registered secret value plus a key=value token.
DEFAULT_SEAM_OVERLAP = 4 * 1024
# Cap on a single console-stream read so a chatty guest cannot grow the buffer unboundedly
# between rotations.
DEFAULT_READ_CHUNK = 16 * 1024

# The same crash signature `classify_console` keys on, duplicated here (no core import from a
# provider): a crash marker forces an immediate rotation so the panic tail is flushed promptly.
_CRASH_MARKER = re.compile(
    rb"(?i)kernel panic|BUG: unable to handle|Oops:|Call Trace:|general protection fault"
)


class ConsoleStream(Protocol):
    """The slice of a ``virDomainOpenConsole`` stream the collector reads.

    ``recv`` returns up to ``nbytes`` of console output, ``b""`` on a clean end, and raises
    on a dropped stream (the collector reconnects). ``close`` releases the stream.
    """

    def recv(self, nbytes: int) -> bytes: ...
    def close(self) -> None: ...


class OpenConsole(Protocol):
    """Open a fresh console stream for a System (wraps ``virDomainOpenConsole``)."""

    def __call__(self, system_id: UUID, /) -> ConsoleStream: ...


class ConsolePartStore(Protocol):
    """The narrow object-store port for numbered console parts and the final artifact.

    Parts are small redacted objects keyed by ``(system_id, index)``; ``finalize`` writes the
    single concatenated console artifact downstream consumers read.
    """

    def put_part(self, system_id: UUID, index: int, data: bytes) -> None: ...
    def list_part_indices(self, system_id: UUID) -> list[int]: ...
    def read_part(self, system_id: UUID, index: int) -> bytes: ...
    def write_console_artifact(self, system_id: UUID, data: bytes) -> None: ...
    def delete_part(self, system_id: UUID, index: int) -> None: ...


class ConsoleCollector:
    """Streams one System's console, rotating redacted parts and assembling on finalize.

    The collector is driven step-wise by the hosting loop: :meth:`pump_once` reads one chunk
    (reconnecting on a drop) and rotates when the buffer crosses the threshold or a crash
    marker appears; :meth:`finalize` flushes the tail and assembles the single artifact. The
    instance is single-task — the hosting loop runs exactly one pump task per System.
    """

    def __init__(
        self,
        system_id: UUID,
        *,
        open_console: OpenConsole,
        store: ConsolePartStore,
        secret_registry: SecretRegistry,
        rotation_threshold: int = DEFAULT_ROTATION_THRESHOLD,
        seam_overlap: int = DEFAULT_SEAM_OVERLAP,
        read_chunk: int = DEFAULT_READ_CHUNK,
    ) -> None:
        self._system_id = system_id
        self._open_console = open_console
        self._store = store
        self._secret_registry = secret_registry
        self._rotation_threshold = rotation_threshold
        self._seam_overlap = seam_overlap
        self._read_chunk = read_chunk
        self._stream: ConsoleStream | None = None
        self._buffer = bytearray()
        # Raw trailing bytes held back from the previous rotation and prepended to the next
        # part, so a secret straddling the size threshold is redacted as one contiguous run and
        # never uploaded raw in either part (AC2). Carried across rotations, flushed at finalize.
        self._carry = b""
        # The next part index resumes past any parts a prior collector left (a restart after a
        # dead stream must not overwrite already-uploaded parts).
        self._next_index = self._resume_index()
        self._finalized = False

    @property
    def system_id(self) -> UUID:
        return self._system_id

    def _resume_index(self) -> int:
        existing = self._store.list_part_indices(self._system_id)
        return (max(existing) + 1) if existing else 0

    def _ensure_stream(self) -> ConsoleStream:
        if self._stream is None:
            self._stream = self._open_console(self._system_id)
        return self._stream

    def _drop_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001 - a dropped stream may fail to close; reconnect anyway
                _log.debug("closing dropped console stream for %s failed", self._system_id)
            self._stream = None

    def pump_once(self) -> bool:
        """Read one chunk into the buffer, rotating as needed; return whether bytes arrived.

        Reconnects on a stream drop (AC1) and rotates on the size threshold or a crash marker
        (AC2). A clean end-of-stream (``b""``) drops the stream so the next pump reconnects —
        a powered-off guest reconnects when it boots again.
        """
        stream = self._ensure_stream()
        try:
            chunk = stream.recv(self._read_chunk)
        except Exception:  # noqa: BLE001 - any stream error is a drop; reconnect on the next pump
            _log.info("console stream for %s dropped; will reconnect", self._system_id)
            self._drop_stream()
            return False
        if not chunk:
            self._drop_stream()
            return False
        self._buffer.extend(chunk)
        self._maybe_rotate()
        return True

    def _maybe_rotate(self) -> None:
        if _CRASH_MARKER.search(self._buffer):
            # The panic tail is the highest-value, most-at-risk part; flush it fully and
            # immediately (no overlap held back) so a hard reconciler kill loses the least
            # (ADR-0095 Consequences). Seam-scanning past a crash is the lesser concern.
            self._flush_tail()
            return
        if len(self._buffer) >= self._rotation_threshold:
            self._rotate()

    def _rotate(self) -> None:
        """Upload one part: the carried-over overlap + buffer, holding back a fresh overlap.

        The held-back ``seam_overlap`` raw bytes stay in ``_carry`` for the next part, so a
        secret straddling the threshold is redacted contiguously and never uploaded raw.
        """
        data = self._carry + bytes(self._buffer)
        self._buffer.clear()
        if not data:
            return
        if len(data) > self._seam_overlap:
            split = len(data) - self._seam_overlap
            to_upload, self._carry = data[:split], data[split:]
        else:
            # Smaller than the overlap: keep it all carried so the next part still scans
            # across the join; nothing is uploaded raw this rotation.
            to_upload, self._carry = b"", data
        if not to_upload:
            return
        self._store.put_part(self._system_id, self._next_index, self._redact(to_upload))
        self._next_index += 1

    def _flush_tail(self) -> None:
        """Upload the held-back carry + buffer as the final part (no overlap held back)."""
        data = self._carry + bytes(self._buffer)
        self._buffer.clear()
        self._carry = b""
        if not data:
            return
        self._store.put_part(self._system_id, self._next_index, self._redact(data))
        self._next_index += 1

    def _redact(self, data: bytes) -> bytes:
        """Redact ``data`` before it leaves the worker (untrusted guest bytes, ADR-0027).

        The redactor seeds from the registry per call so a value registered after the collector
        started is still caught. Non-UTF-8 console bytes decode with ``errors="replace"`` so a
        partial multibyte tail never raises.
        """
        redactor = Redactor(registry=self._secret_registry)
        return redactor.redact_text(data.decode("utf-8", "replace")).encode("utf-8")

    def finalize(self) -> None:
        """Flush the tail part and assemble the ordered parts into one console artifact.

        Idempotent: a second finalize (capture then teardown) is a no-op once the artifact is
        written. Called by the hosting loop on capture or teardown; the reap supervisor waits
        for this to complete before dropping the collector (AC7 — reap never races finalize).
        """
        if self._finalized:
            return
        self._drop_stream()
        self._flush_tail()
        assembled = bytearray()
        for index in sorted(self._store.list_part_indices(self._system_id)):
            assembled.extend(self._store.read_part(self._system_id, index))
        self._store.write_console_artifact(self._system_id, bytes(assembled))
        self._finalized = True
        _log.info("console collector for %s finalized into one artifact", self._system_id)

    @property
    def finalized(self) -> bool:
        return self._finalized

    def close(self) -> None:
        """Close the console stream **without** finalizing (lock-loss / failover path).

        The artifact is intentionally not assembled — on leader failover the new leader
        cold-starts and pre-failover console history is the accepted best-effort loss
        (ADR-0095). Use :meth:`finalize` for the capture/teardown path that must persist.
        """
        self._drop_stream()
