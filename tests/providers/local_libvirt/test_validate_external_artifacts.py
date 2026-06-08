"""External-artifact validation (ADR-0048 §5)."""

from __future__ import annotations

import struct

import pytest

from kdive.components.requirements import ConfigRequirements
from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.build_validation import (
    extract_build_id_ranged,
    validate_external_artifacts,
)
from kdive.store.objectstore import HeadResult

_BZIMAGE_HEAD = b"\x00" * 0x202 + b"HdrS"  # bzImage magic at offset 0x202


class _FakeStore:
    def __init__(self, blobs: dict[str, bytes], heads: dict[str, HeadResult]) -> None:
        self._blobs = blobs
        self._heads = heads
        self.range_calls: list[tuple[str, int, int]] = []

    def head(self, key: str) -> HeadResult | None:
        return self._heads.get(key)

    def get_range(self, key: str, *, start: int, length: int) -> bytes:
        self.range_calls.append((key, start, length))
        return self._blobs[key][start : start + length]


def _elf_with_build_id(build_id: bytes) -> bytes:
    """Minimal ELF64-LE blob carrying a .note.gnu.build-id section.

    Layout (offsets chosen so extract_build_id_ranged round-trips):
      [0:64]   ELF64 header
      then     note section bytes, shstrtab bytes, section header table
    """
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\x00" + build_id
    # section-name string table: index 0 = "", then the two section names.
    shstrtab = b"\x00.shstrtab\x00.note.gnu.build-id\x00"
    name_shstrtab = shstrtab.index(b".shstrtab")
    name_note = shstrtab.index(b".note.gnu.build-id")

    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    # We'll lay out: header(64) | note | shstrtab | SHT
    note_off = 64
    shstr_off = note_off + len(note)
    sht_off = shstr_off + len(shstrtab)
    e_shentsize = 64
    e_shnum = 3  # null, .note.gnu.build-id, .shstrtab
    e_shstrndx = 2  # .shstrtab is section index 2
    struct.pack_into("<Q", header, 0x28, sht_off)  # e_shoff
    struct.pack_into("<H", header, 0x3A, e_shentsize)  # e_shentsize
    struct.pack_into("<H", header, 0x3C, e_shnum)  # e_shnum
    struct.pack_into("<H", header, 0x3E, e_shstrndx)  # e_shstrndx

    def section(sh_name: int, sh_type: int, sh_offset: int, sh_size: int) -> bytes:
        sh = bytearray(64)
        struct.pack_into("<I", sh, 0x00, sh_name)
        struct.pack_into("<I", sh, 0x04, sh_type)
        struct.pack_into("<Q", sh, 0x18, sh_offset)
        struct.pack_into("<Q", sh, 0x20, sh_size)
        return bytes(sh)

    sht = (
        section(0, 0, 0, 0)  # SHN_UNDEF
        + section(name_note, 7, note_off, len(note))  # SHT_NOTE
        + section(name_shstrtab, 3, shstr_off, len(shstrtab))  # SHT_STRTAB
    )
    return bytes(header) + note + shstrtab + sht


def test_missing_kernel_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(store, manifest=[], keys={}, declared_build_id=None)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_missing_object_is_configuration_error() -> None:
    store = _FakeStore({}, {})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", 6)],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_checksum_mismatch_is_build_failure() -> None:
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD},
        {"k": HeadResult(size_bytes=len(_BZIMAGE_HEAD), checksum_sha256="OTHER", etag="e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", len(_BZIMAGE_HEAD))],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_bad_kernel_magic_is_build_failure() -> None:
    bad = b"\x00" * 0x300
    store = _FakeStore({"k": bad}, {"k": HeadResult(len(bad), "csum", "e")})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "csum", len(bad))],
            keys={"kernel": "k"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_happy_path_kernel_only_returns_build_output() -> None:
    store = _FakeStore({"k": _BZIMAGE_HEAD}, {"k": HeadResult(len(_BZIMAGE_HEAD), "csum", "e")})
    out = validate_external_artifacts(
        store,
        manifest=[ManifestEntry("kernel", "csum", len(_BZIMAGE_HEAD))],
        keys={"kernel": "k"},
        declared_build_id=None,
    )
    assert (
        out.output.kernel_ref == "k"
        and out.output.debuginfo_ref == ""
        and out.output.build_id == ""
    )
    assert set(out.heads) == {"kernel"}


def test_build_id_mismatch_is_build_failure() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
                ManifestEntry("vmlinux", "cv", len(blob)),
            ],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id="beef",
        )
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_vmlinux_without_declared_build_id_is_configuration_error() -> None:
    blob = _elf_with_build_id(bytes.fromhex("dead"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
                ManifestEntry("vmlinux", "cv", len(blob)),
            ],
            keys={"kernel": "k", "vmlinux": "v"},
            declared_build_id=None,
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_matching_build_id_passes_and_pairs_vmlinux() -> None:
    blob = _elf_with_build_id(bytes.fromhex("deadbeef"))
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="DEADBEEF",  # case-insensitive vs the lowercase-hex note
    )
    assert out.output.kernel_ref == "k" and out.output.debuginfo_ref == "v"
    assert out.output.build_id == "deadbeef"


def test_initrd_is_validated_and_returned_in_keys() -> None:
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "i": b"\x1f\x8b" + b"\x00" * 40},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "i": HeadResult(42, "ci", "e")},
    )
    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
            ManifestEntry("initrd", "ci", 42),
        ],
        keys={"kernel": "k", "initrd": "i"},
        declared_build_id=None,
    )
    assert out.output.kernel_ref == "k"
    assert set(out.heads) == {"kernel", "initrd"}


def test_effective_config_satisfies_profile_requirements() -> None:
    config = b"CONFIG_VIRTIO_BLK=y\n"
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "c": config},
        {
            "k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"),
            "c": HeadResult(len(config), "cc", "ec"),
        },
    )

    out = validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
            ManifestEntry("effective_config", "cc", len(config)),
        ],
        keys={"kernel": "k", "effective_config": "c"},
        declared_build_id=None,
        profile_requirements=ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
    )

    assert set(out.heads) == {"kernel", "effective_config"}


def test_effective_config_required_when_profile_requirements_selected() -> None:
    store = _FakeStore({"k": _BZIMAGE_HEAD}, {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e")})

    with pytest.raises(CategorizedError) as caught:
        validate_external_artifacts(
            store,
            manifest=[ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD))],
            keys={"kernel": "k"},
            declared_build_id=None,
            profile_requirements=ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_oversized_effective_config_is_configuration_error_without_read() -> None:
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "c": b""},
        {
            "k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"),
            "c": HeadResult(1024 * 1024 + 1, "cc", "ec"),
        },
    )

    with pytest.raises(CategorizedError) as caught:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
                ManifestEntry("effective_config", "cc", 1024 * 1024 + 1),
            ],
            keys={"kernel": "k", "effective_config": "c"},
            declared_build_id=None,
            profile_requirements=ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ("c", 0, 1024 * 1024 + 1) not in store.range_calls


def test_effective_config_mismatch_is_configuration_error() -> None:
    config = b"CONFIG_VIRTIO_BLK=n\n"
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "c": config},
        {
            "k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"),
            "c": HeadResult(len(config), "cc", "ec"),
        },
    )

    with pytest.raises(CategorizedError) as caught:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
                ManifestEntry("effective_config", "cc", len(config)),
            ],
            keys={"kernel": "k", "effective_config": "c"},
            declared_build_id=None,
            profile_requirements=ConfigRequirements(required={"CONFIG_VIRTIO_BLK": "y"}),
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_vmlinux_without_upload_key_is_configuration_error() -> None:
    store = _FakeStore({"k": _BZIMAGE_HEAD}, {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e")})
    with pytest.raises(CategorizedError) as e:
        validate_external_artifacts(
            store,
            manifest=[
                ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
                ManifestEntry("vmlinux", "cv", 64),
            ],
            keys={"kernel": "k"},  # vmlinux declared but no upload key
            declared_build_id="deadbeef",
        )
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


def _validate_vmlinux_blob(blob: bytes) -> None:
    """Wire a vmlinux blob through validate_external_artifacts to reach the extractor."""
    store = _FakeStore(
        {"k": _BZIMAGE_HEAD, "v": blob},
        {"k": HeadResult(len(_BZIMAGE_HEAD), "ck", "e"), "v": HeadResult(len(blob), "cv", "e")},
    )
    validate_external_artifacts(
        store,
        manifest=[
            ManifestEntry("kernel", "ck", len(_BZIMAGE_HEAD)),
            ManifestEntry("vmlinux", "cv", len(blob)),
        ],
        keys={"kernel": "k", "vmlinux": "v"},
        declared_build_id="deadbeef",
    )


def test_truncated_elf_header_is_build_failure() -> None:
    blob = b"\x7fELF\x02\x01" + b"\x00" * 2  # passes magic/class/endian, header < 64 bytes
    with pytest.raises(CategorizedError) as e:
        _validate_vmlinux_blob(blob)
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_shstrndx_past_shnum_is_build_failure() -> None:
    blob = bytearray(_elf_with_build_id(bytes.fromhex("deadbeef")))
    e_shnum = struct.unpack_from("<H", blob, 0x3C)[0]
    struct.pack_into("<H", blob, 0x3E, e_shnum + 5)  # e_shstrndx points past the SHT
    with pytest.raises(CategorizedError) as e:
        _validate_vmlinux_blob(bytes(blob))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_note_sh_name_past_shstrtab_is_build_failure() -> None:
    blob = bytearray(_elf_with_build_id(bytes.fromhex("deadbeef")))
    e_shoff = struct.unpack_from("<Q", blob, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", blob, 0x3A)[0]
    note_sh_name_off = e_shoff + 1 * e_shentsize  # section index 1 is the SHT_NOTE entry
    struct.pack_into("<I", blob, note_sh_name_off, 0xFFFF)  # sh_name far past the shstrtab
    with pytest.raises(CategorizedError) as e:
        _validate_vmlinux_blob(bytes(blob))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def test_extract_build_id_ranged_truncated_header_is_build_failure() -> None:
    blob = b"\x7fELF\x02\x01"
    store = _FakeStore({"v": blob}, {})
    with pytest.raises(CategorizedError) as e:
        extract_build_id_ranged(store, "v", max_size=len(blob))
    assert e.value.category is ErrorCategory.BUILD_FAILURE


def _tamper_note_sh_size(blob: bytes, sh_size: int) -> bytes:
    """Return ``blob`` with the .note.gnu.build-id section's sh_size overwritten.

    Section index 1 in ``_elf_with_build_id`` is the SHT_NOTE entry; sh_size lives at
    offset 0x20 within its 64-byte SHT entry. Tampering the SHT (which trails the data)
    leaves ``len(blob)`` — hence the head's declared size — unchanged.
    """
    mutable = bytearray(blob)
    e_shoff = struct.unpack_from("<Q", mutable, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", mutable, 0x3A)[0]
    struct.pack_into("<Q", mutable, e_shoff + 1 * e_shentsize + 0x20, sh_size)
    return bytes(mutable)


def test_oversized_section_header_table_is_build_failure() -> None:
    # e_shentsize*e_shnum past the 16 MiB cap, but the header is within a large max_size so
    # the per-object guard passes — the absolute SHT cap must catch it before the get_range.
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    struct.pack_into("<Q", header, 0x28, 64)  # e_shoff
    struct.pack_into("<H", header, 0x3A, 512)  # e_shentsize
    struct.pack_into("<H", header, 0x3C, 0xFFFF)  # e_shnum -> 512*65535 == 32 MiB > 16 MiB
    struct.pack_into("<H", header, 0x3E, 0)  # e_shstrndx
    store = _FakeStore({"v": bytes(header)}, {})
    with pytest.raises(CategorizedError) as e:
        extract_build_id_ranged(store, "v", max_size=64 * 1024 * 1024)
    assert e.value.category is ErrorCategory.BUILD_FAILURE
    # Tie the assertion to the SHT cap specifically: only that guard sets ``sht_bytes``.
    # Without it the empty fake-store SHT read would still raise BUILD_FAILURE via a
    # struct.error, so a bare category check would pass even with the guard removed.
    assert e.value.details.get("sht_bytes") == 512 * 0xFFFF


def test_oversized_section_size_is_build_failure() -> None:
    base = _elf_with_build_id(bytes.fromhex("deadbeef"))
    # Past the object size (max_size == len(blob)) but under the per-section cap.
    past_object = _tamper_note_sh_size(base, len(base) + 1)
    # Past the per-section cap (16 MiB).
    past_cap = _tamper_note_sh_size(base, 17 * 1024 * 1024)
    for blob in (past_object, past_cap):
        with pytest.raises(CategorizedError) as e:
            _validate_vmlinux_blob(blob)
        assert e.value.category is ErrorCategory.BUILD_FAILURE
