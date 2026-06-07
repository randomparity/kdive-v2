"""Provider-neutral validation for externally uploaded build artifacts."""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from typing import Protocol

from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import BuildOutput, ValidatedUpload
from kdive.store.objectstore import HeadResult

_NT_GNU_BUILD_ID = 3
_ELF_MAGIC = b"\x7fELF"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7
_MAX_SECTION_BYTES = 16 * 1024 * 1024


class ValidatorStore(Protocol):
    """Object-store operations needed by external build validation."""

    def head(self, key: str) -> HeadResult | None: ...
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


def validate_external_artifacts(
    store: ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
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
    if head.size_bytes != entry.size_bytes or head.checksum_sha256 != entry.sha256:
        raise _build_failure("uploaded artifact disagrees with its manifest", name=name)
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
