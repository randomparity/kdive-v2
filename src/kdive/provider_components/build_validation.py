"""Provider-neutral validation for externally uploaded build artifacts."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import HeadResult, chunk_key
from kdive.provider_components.build_results import BuildOutput, ValidatedUpload
from kdive.provider_components.requirements import ConfigRequirements, validate_config_requirements
from kdive.provider_components.uploads import ManifestEntry

_NT_GNU_BUILD_ID = 3
_ELF_MAGIC = b"\x7fELF"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7
_MAX_SECTION_BYTES = 16 * 1024 * 1024
_MAX_EFFECTIVE_CONFIG_BYTES = 1024 * 1024


class HeadStore(Protocol):
    """The minimal object-store surface chunk HEAD-verification needs."""

    def head(self, key: str) -> HeadResult | None: ...


class ValidatorStore(HeadStore, Protocol):
    """Object-store operations needed by external build validation."""

    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


def parse_gnu_build_id(notes: bytes) -> str:
    """Extract the GNU build-id (lowercase hex) from a little-endian ELF note blob."""
    offset = 0
    end = len(notes)
    while offset + 12 <= end:
        namesz = int.from_bytes(notes[offset : offset + 4], "little")
        descsz = int.from_bytes(notes[offset + 4 : offset + 8], "little")
        note_type = int.from_bytes(notes[offset + 8 : offset + 12], "little")
        name_start = offset + 12
        name_end = name_start + namesz
        desc_start = name_end + (-namesz % 4)
        desc_end = desc_start + descsz
        if desc_end > end:
            break
        name = notes[name_start:name_end].rstrip(b"\x00")
        if note_type == _NT_GNU_BUILD_ID and name == b"GNU":
            return notes[desc_start:desc_end].hex()
        next_offset = desc_end + (-descsz % 4)
        if next_offset <= offset:
            break
        offset = next_offset
    raise CategorizedError(
        "vmlinux carries no GNU build-id note",
        category=ErrorCategory.BUILD_FAILURE,
    )


def patch_target_paths(patch_text: str, *, strip: int = 1) -> set[Path]:
    """Parse the workspace-relative file paths a unified diff touches.

    Collects both the pre-image (``--- a/...``) and post-image (``+++ b/...``) sides so
    created, modified, and deleted files are all covered, applying ``-p<strip>`` component
    stripping (``strip=1`` drops the leading ``a/``/``b/``). The ``/dev/null`` side of an
    add or delete, and any path shallower than ``strip``, are ignored.

    Used to verify ``git apply`` actually changed the tree: a ``.git``-less build workspace
    can make ``git apply`` exit 0 while silently skipping the patch (issue #227), so the
    caller snapshots these paths before and after applying and fails if none changed.
    """
    paths: set[Path] = set()
    for line in patch_text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        spec = line[4:].split("\t", 1)[0].strip()
        # git c-quotes paths with special/non-ASCII bytes ("b/...", octal escapes); decoding
        # them here would be brittle, so skip them — the caller's `git apply` stderr check
        # still catches a skipped quoted path, and we avoid wrongly flagging an applied one.
        if not spec or spec == "/dev/null" or spec.startswith('"'):
            continue
        components = spec.split("/")
        if len(components) <= strip:
            continue
        paths.add(Path(*components[strip:]))
    return paths


def snapshot_file_bytes(path: Path) -> bytes | None:
    """Return ``path`` contents, or ``None`` if it does not exist or cannot be read.

    Used by the build planes to snapshot a patch's target files before and after
    ``git apply`` and detect a silent no-op apply (issue #227).
    """
    try:
        return path.read_bytes()
    except OSError:
        return None


def validate_external_artifacts(
    store: ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
    profile_requirements: ConfigRequirements | None = None,
) -> ValidatedUpload:
    """Validate uploaded build artifacts; return the ``BuildOutput`` plus object heads."""
    by_name = {e.name: e for e in manifest}
    if "kernel" not in by_name or "kernel" not in keys:
        raise CategorizedError(
            "external build is missing the required kernel artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    heads: dict[str, HeadResult] = {}
    for name, entry in by_name.items():
        key = keys.get(name)
        if key is None:
            raise CategorizedError(
                f"declared artifact {name!r} has no upload key",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": name},
            )
        heads[name] = _validate_one_artifact(store, name, entry, key)
    if profile_requirements is not None:
        _validate_effective_config(
            store,
            keys=keys,
            heads=heads,
            profile_requirements=profile_requirements,
        )

    build_id = ""
    if "vmlinux" in by_name:
        if not declared_build_id:
            raise CategorizedError(
                "a vmlinux upload requires a declared build_id",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        actual = extract_build_id_ranged(
            store, keys["vmlinux"], max_size=heads["vmlinux"].size_bytes
        )
        if actual != declared_build_id.lower():
            raise _build_failure("declared build_id does not match the uploaded vmlinux")
        build_id = actual

    output = BuildOutput(
        kernel_ref=keys["kernel"],
        debuginfo_ref=keys.get("vmlinux", ""),
        build_id=build_id,
    )
    return ValidatedUpload(output=output, heads=heads)


def _validate_effective_config(
    store: ValidatorStore,
    *,
    keys: Mapping[str, str],
    heads: Mapping[str, HeadResult],
    profile_requirements: ConfigRequirements,
) -> None:
    key = keys.get("effective_config")
    head = heads.get("effective_config")
    if key is None or head is None:
        raise CategorizedError(
            "external build profile requirements need an effective_config artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if head.size_bytes > _MAX_EFFECTIVE_CONFIG_BYTES:
        raise CategorizedError(
            "effective_config exceeds the readable size cap",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "name": "effective_config",
                "size_bytes": head.size_bytes,
                "max_size_bytes": _MAX_EFFECTIVE_CONFIG_BYTES,
            },
        )
    data = store.get_range(key, start=0, length=head.size_bytes)
    validate_config_requirements(data.decode("utf-8", errors="replace"), profile_requirements)


def extract_build_id_ranged(store: ValidatorStore, key: str, *, max_size: int) -> str:
    """Extract a vmlinux GNU build-id via bounded ranged ELF64-LE reads."""
    header = store.get_range(key, start=0, length=64)
    if len(header) < 64:
        raise _build_failure("vmlinux ELF header is truncated")
    if header[:4] != _ELF_MAGIC or header[4] != 2 or header[5] != 1:
        raise _build_failure("vmlinux is not a 64-bit little-endian ELF")
    try:
        e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
        e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
        e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
        e_shstrndx = struct.unpack_from("<H", header, 0x3E)[0]
        if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64:
            raise _build_failure("vmlinux has no usable section header table")
        if e_shentsize * e_shnum > _MAX_SECTION_BYTES:
            raise _build_failure(
                "vmlinux section header table exceeds the readable cap",
                sht_bytes=e_shentsize * e_shnum,
            )
        if e_shoff + e_shentsize * e_shnum > max_size:
            raise _build_failure("vmlinux section header table extends past the object size")
        sht = store.get_range(key, start=e_shoff, length=e_shentsize * e_shnum)
        shstr = _read_section(store, key, sht, e_shentsize, e_shstrndx, max_size=max_size)
        return _find_build_id_note(store, key, sht, shstr, e_shentsize, e_shnum, max_size=max_size)
    except (struct.error, ValueError, IndexError) as exc:
        raise _build_failure("vmlinux ELF is structurally malformed") from exc


def verify_chunks(store: HeadStore, prefix: str, entry: ManifestEntry) -> None:
    """HEAD-verify each declared chunk's stored ``(size, sha256)`` before reassembly.

    For a chunked artifact the per-chunk SHA-256 pins are the integrity anchor (ADR-0104 §4):
    each chunk object's stored checksum and size must match the manifest before the chunks are
    reassembled into the final object.

    Raises:
        CategorizedError: a chunk was never uploaded
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or disagrees with its manifest entry
            (:attr:`ErrorCategory.BUILD_FAILURE`).
    """
    assert entry.chunks is not None
    for part_number, chunk in enumerate(entry.chunks, start=1):
        key = chunk_key(prefix, entry.name, part_number)
        head = store.head(key)
        if head is None:
            raise CategorizedError(
                f"declared chunk {part_number} of {entry.name!r} was never uploaded",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"name": entry.name, "part_number": part_number},
            )
        if head.size_bytes != chunk.size_bytes or head.checksum_sha256 != chunk.sha256:
            raise _build_failure(
                "uploaded chunk disagrees with its manifest",
                name=entry.name,
                part_number=part_number,
            )


def _validate_one_artifact(
    store: ValidatorStore, name: str, entry: ManifestEntry, key: str
) -> HeadResult:
    head = store.head(key)
    if head is None:
        raise CategorizedError(
            f"declared artifact {name!r} was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"name": name},
        )
    if entry.chunks is None:
        if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
            raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
    elif head.size_bytes != entry.size_bytes:
        # The reassembled multipart object exposes only a composite checksum, so the
        # whole-object SHA-256 is not comparable here; the per-chunk pins (verify_chunks)
        # already bound every byte. Only the total size is checked on the final object.
        raise _build_failure("reassembled artifact size disagrees with its manifest", name=name)
    _check_magic(store, name, key)
    return head


def _check_magic(store: ValidatorStore, name: str, key: str) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        magic = store.get_range(key, start=_BZIMAGE_MAGIC_OFFSET, length=4)
        if magic != _BZIMAGE_MAGIC:
            raise _build_failure("kernel is not a bzImage", name=name)


def _find_build_id_note(
    store: ValidatorStore,
    key: str,
    sht: bytes,
    shstr: bytes,
    e_shentsize: int,
    e_shnum: int,
    *,
    max_size: int,
) -> str:
    for i in range(e_shnum):
        off = i * e_shentsize
        sh_name = struct.unpack_from("<I", sht, off)[0]
        sh_type = struct.unpack_from("<I", sht, off + 4)[0]
        if sh_type != _SHT_NOTE:
            continue
        name = shstr[sh_name : shstr.index(b"\x00", sh_name)]
        if name == b".note.gnu.build-id":
            notes = _read_section(store, key, sht, e_shentsize, i, max_size=max_size)
            return parse_gnu_build_id(notes)
    raise _build_failure("vmlinux carries no .note.gnu.build-id section")


def _read_section(
    store: ValidatorStore, key: str, sht: bytes, e_shentsize: int, index: int, *, max_size: int
) -> bytes:
    off = index * e_shentsize
    sh_offset = struct.unpack_from("<Q", sht, off + 0x18)[0]
    sh_size = struct.unpack_from("<Q", sht, off + 0x20)[0]
    if sh_size > _MAX_SECTION_BYTES:
        raise _build_failure("vmlinux section exceeds the readable-section cap", sh_size=sh_size)
    if sh_offset + sh_size > max_size:
        raise _build_failure(
            "vmlinux section extends past the object size", sh_offset=sh_offset, sh_size=sh_size
        )
    return store.get_range(key, start=sh_offset, length=sh_size)


def _build_failure(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.BUILD_FAILURE, details=details)
