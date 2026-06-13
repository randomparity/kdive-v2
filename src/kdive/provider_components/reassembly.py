"""Server-side reassembly of a chunked artifact into one object (ADR-0104 §1, §4)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import HeadResult, chunk_key
from kdive.provider_components.build_validation import verify_chunks
from kdive.provider_components.uploads import ManifestEntry


class ReassemblyStore(Protocol):
    """The object-store ops reassembly needs (HEAD + the four multipart primitives)."""

    def head(self, key: str) -> HeadResult | None: ...
    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str: ...
    def upload_part_copy(
        self, key: str, upload_id: str, *, part_number: int, source_key: str
    ) -> str: ...
    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> str: ...
    def abort_multipart_upload(self, key: str, upload_id: str) -> None: ...


def reassemble_chunked(
    store: ReassemblyStore, *, prefix: str, final_key: str, entry: ManifestEntry
) -> None:
    """HEAD-verify each chunk, then ``Create``/``UploadPartCopy``/``Complete`` the final object.

    The chunks are copied server-side (no bytes transit the process) in declared order into
    ``final_key``. Any failure after the multipart upload is created triggers an
    ``AbortMultipartUpload`` so a caught error leaves no in-progress upload the caller's reaper
    cannot see; the caller maps the raised error to a typed failure and the abandoned-upload
    reaper backstops the chunk objects. The caller runs whole-object validation on
    ``final_key`` after this returns.

    Raises:
        CategorizedError: a chunk fails its HEAD verification (before any multipart call) or a
            multipart operation fails (after which the upload is aborted).
    """
    assert entry.chunks is not None
    verify_chunks(store, prefix, entry)
    upload_id = store.create_multipart_upload(
        final_key, sensitivity=Sensitivity.SENSITIVE, retention_class="build"
    )
    try:
        parts: list[tuple[int, str]] = []
        for part_number in range(1, len(entry.chunks) + 1):
            etag = store.upload_part_copy(
                final_key,
                upload_id,
                part_number=part_number,
                source_key=chunk_key(prefix, entry.name, part_number),
            )
            parts.append((part_number, etag))
        store.complete_multipart_upload(final_key, upload_id, parts)
    except BaseException:
        store.abort_multipart_upload(final_key, upload_id)
        raise
