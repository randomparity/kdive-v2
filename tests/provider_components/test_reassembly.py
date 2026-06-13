"""Server-side chunk reassembly orchestration (ADR-0104 §4)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import HeadResult
from kdive.provider_components.reassembly import reassemble_chunked
from kdive.provider_components.uploads import ChunkEntry, ManifestEntry

_PREFIX = "local/runs/x/"
_FINAL = "local/runs/x/vmlinux"


class _FakeStore:
    def __init__(self, *, fail_copy_at: int | None = None) -> None:
        self.events: list[tuple[object, ...]] = []
        self._fail_copy_at = fail_copy_at

    def head(self, key: str) -> HeadResult | None:
        sizes = {".part0001": (6, "c0"), ".part0002": (4, "c1")}
        for suffix, (size, sha) in sizes.items():
            if key.endswith(suffix):
                return HeadResult(size_bytes=size, checksum_sha256=sha, etag="e")
        return None

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        self.events.append(("create", key))
        return "uid"

    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str:
        if self._fail_copy_at == part_number:
            raise CategorizedError("boom", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        self.events.append(("copy", part_number, source_key))
        return f"etag-{part_number}"

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str:
        self.events.append(("complete", tuple(parts)))
        return "final"

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        self.events.append(("abort", key))


def _entry() -> ManifestEntry:
    return ManifestEntry("vmlinux", "whole", 10, chunks=(ChunkEntry("c0", 6), ChunkEntry("c1", 4)))


def test_reassemble_verifies_copies_in_order_completes() -> None:
    store = _FakeStore()
    reassemble_chunked(store, prefix=_PREFIX, final_key=_FINAL, entry=_entry())
    assert [e[0] for e in store.events] == ["create", "copy", "copy", "complete"]
    assert store.events[1][1] == 1
    assert store.events[2][1] == 2
    assert store.events[3][1] == ((1, "etag-1"), (2, "etag-2"))


def test_reassemble_aborts_on_copy_failure() -> None:
    store = _FakeStore(fail_copy_at=2)
    with pytest.raises(CategorizedError):
        reassemble_chunked(store, prefix=_PREFIX, final_key=_FINAL, entry=_entry())
    assert ("abort", _FINAL) in store.events


def test_reassemble_fails_before_mpu_on_chunk_mismatch() -> None:
    store = _FakeStore()
    bad = ManifestEntry(
        "vmlinux", "whole", 10, chunks=(ChunkEntry("WRONG", 6), ChunkEntry("c1", 4))
    )
    with pytest.raises(CategorizedError):
        reassemble_chunked(store, prefix=_PREFIX, final_key=_FINAL, entry=bad)
    assert store.events == []  # no multipart calls happened
