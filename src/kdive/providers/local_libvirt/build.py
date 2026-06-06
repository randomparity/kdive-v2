"""Local-libvirt Build plane: make a kernel in a warm workspace and store two artifacts (ADR-0029).

`LocalLibvirtBuild` checks out a warm source tree (base ref + the profile's optional
patch), preflights the resolved ``.config`` for the kdump/debuginfo prerequisites, runs
``make`` incrementally, extracts the produced ``vmlinux``'s GNU build-id, and stores two
``sensitive`` artifacts under deterministic Run-keyed object keys — the bootable kernel
image (`kernel`) and the ``vmlinux``/debuginfo (`vmlinux`). It returns both object keys
plus the build-id (:class:`BuildOutput`).

The slow, environment-bound operations (warm-tree checkout, ``.config`` read, ``make``,
ELF reads, build-id extraction) are **injected seams** that default to the real
implementations, so unit tests cover the orchestration/error contract without a
toolchain; the real ``make`` path is exercised under the ``live_vm`` gate. `build()` is
synchronous; the async build handler offloads the whole call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import os
import struct
import subprocess  # noqa: S404 - make is invoked with a fixed argv, no shell
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol
from uuid import UUID

from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.store.objectstore import HeadResult, StoredArtifact, object_store_from_env

_WORKSPACE_ENV = "KDIVE_BUILD_WORKSPACE"
_KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"
_DEFAULT_WORKSPACE = "/var/lib/kdive/build"
_RETENTION_CLASS = "build"
_NT_GNU_BUILD_ID = 3
_ELF_MAGIC = b"\x7fELF"
_BZIMAGE_MAGIC = b"HdrS"
_BZIMAGE_MAGIC_OFFSET = 0x202
_SHT_NOTE = 7

# The kdump prerequisite is satisfied by CONFIG_CRASH_DUMP; symbolization needs DWARF or
# BTF debuginfo. Each tuple is an OR-group: the config must enable at least one of each.
_REQUIRED_CONFIG: tuple[tuple[str, ...], ...] = (
    ("CONFIG_CRASH_DUMP",),
    ("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),
)


class BuildOutput(NamedTuple):
    """A build result: the two object-store keys plus the kernel's GNU build-id."""

    kernel_ref: str
    debuginfo_ref: str
    build_id: str


class Builder(Protocol):
    """The handler-facing build port (the realized M0 contract, ADR-0029 §4).

    Distinct from :class:`kdive.providers.interfaces.BuildPlane`, the capability-dispatch
    placeholder that returns a single artifact: the realized port stores **two** artifacts
    and returns both refs plus the build-id the symbolization planes key on.
    """

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput: ...


class _StorePort(Protocol):
    def put_artifact(
        self,
        tenant: str,
        kind: str,
        object_id: str,
        name: str,
        *,
        data: bytes,
        sensitivity: Sensitivity,
        retention_class: str,
    ) -> StoredArtifact: ...


def parse_gnu_build_id(notes: bytes) -> str:
    """Extract the GNU build-id (lowercase hex) from a little-endian ELF note blob.

    Walks the ``.note.gnu.build-id`` note stream — each note is ``namesz``/``descsz``/
    ``type`` (4-byte LE each), a 4-byte-aligned name, then a 4-byte-aligned desc — and
    returns the desc of the first ``NT_GNU_BUILD_ID`` note whose name is ``GNU``.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if no GNU build-id note is present (a
            ``vmlinux`` without one cannot be symbolized against — a build defect).
    """
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
            break  # truncated/corrupt note stream — treat as no build-id
        name = notes[name_start:name_end].rstrip(b"\x00")
        if note_type == _NT_GNU_BUILD_ID and name == b"GNU":
            return notes[desc_start:desc_end].hex()
        next_offset = desc_end + (-descsz % 4)
        if next_offset <= offset:
            break  # defensive: a note must advance the cursor (never loop on a malformed one)
        offset = next_offset
    raise CategorizedError(
        "vmlinux carries no GNU build-id note",
        category=ErrorCategory.BUILD_FAILURE,
    )


def _missing_config_groups(config_text: str) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text`` (``CONFIG_X=y``)."""
    enabled = {
        line.split("=", 1)[0]
        for line in config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith("=y")
    }
    return [group for group in _REQUIRED_CONFIG if not any(opt in enabled for opt in group)]


type _Checkout = Callable[[UUID, ServerBuildProfile, Path], None]
type _ReadConfig = Callable[[Path], str]
type _RunMake = Callable[[Path], int]
type _ReadBytes = Callable[[Path], bytes]
type _ReadBuildId = Callable[[Path], str]


class LocalLibvirtBuild:
    """The realized Build port: warm-tree ``make`` + two-artifact store (ADR-0029 §5)."""

    def __init__(
        self,
        *,
        tenant: str,
        workspace_root: Path,
        store_factory: Callable[[], _StorePort],
        checkout: _Checkout,
        read_config: _ReadConfig,
        run_make: _RunMake,
        read_kernel_image: _ReadBytes,
        read_vmlinux: _ReadBytes,
        read_build_id: _ReadBuildId,
    ) -> None:
        self._tenant = tenant
        self._workspace_root = workspace_root
        self._store_factory = store_factory
        self._store: _StorePort | None = None
        self._checkout = checkout
        self._read_config = read_config
        self._run_make = run_make
        self._read_kernel_image = read_kernel_image
        self._read_vmlinux = read_vmlinux
        self._read_build_id = read_build_id

    @classmethod
    def from_env(cls) -> LocalLibvirtBuild:
        """Build from the ``KDIVE_*`` environment; does not spawn ``make`` or connect S3.

        Reads the workspace root (``KDIVE_BUILD_WORKSPACE``) and the warm source tree
        (``KDIVE_KERNEL_SRC``). The object store is built lazily from the ``KDIVE_S3_*``
        env on the first ``build()``, so the worker registers its handler without S3 env
        present. The seams default to the real subprocess/ELF implementations, which run
        only when ``build()`` is called.
        """
        workspace_root = Path(os.environ.get(_WORKSPACE_ENV, _DEFAULT_WORKSPACE))
        kernel_src = os.environ.get(_KERNEL_SRC_ENV, "")
        return cls(
            tenant="local",
            workspace_root=workspace_root,
            store_factory=object_store_from_env,
            checkout=_make_checkout(kernel_src),
            read_config=_real_read_config,
            run_make=_real_run_make,
            read_kernel_image=lambda ws: (ws / "arch/x86/boot/bzImage").read_bytes(),
            read_vmlinux=lambda ws: (ws / "vmlinux").read_bytes(),
            read_build_id=_real_read_build_id,
        )

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store two artifacts; return their refs and the build-id.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the resolved ``.config`` omits a
                kdump/debuginfo prerequisite (checked before ``make``); ``BUILD_FAILURE``
                on a non-zero ``make`` exit or a missing build-id; ``INFRASTRUCTURE_FAILURE``
                propagated from a failed artifact store.
        """
        workspace = self._workspace_root / str(run_id)
        self._checkout(run_id, profile, workspace)
        missing = _missing_config_groups(self._read_config(workspace))
        if missing:
            raise CategorizedError(
                "kernel .config omits a required kdump/debuginfo option",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"missing_any_of": [list(group) for group in missing]},
            )
        if self._run_make(workspace) != 0:
            raise CategorizedError(
                "make exited non-zero",
                category=ErrorCategory.BUILD_FAILURE,
                details={"run_id": str(run_id)},
            )
        build_id = self._read_build_id(workspace)
        kernel = self._put(run_id, "kernel", self._read_kernel_image(workspace))
        vmlinux = self._put(run_id, "vmlinux", self._read_vmlinux(workspace))
        return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)

    def _put(self, run_id: UUID, name: str, data: bytes) -> StoredArtifact:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.put_artifact(
            self._tenant,
            "runs",
            str(run_id),
            name,
            data=data,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        )


def _make_checkout(kernel_src: str) -> _Checkout:
    def _checkout(run_id: UUID, profile: ServerBuildProfile, workspace: Path) -> None:
        _real_checkout(kernel_src, profile, workspace)

    return _checkout


def _real_checkout(  # pragma: no cover - live_vm
    kernel_src: str, profile: ServerBuildProfile, workspace: Path
) -> None:
    raise CategorizedError(
        "real warm-tree checkout runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"kernel_src": kernel_src, "config_ref": profile.config_ref},
    )


def _real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    return (workspace / ".config").read_text()


def _real_run_make(workspace: Path) -> int:  # pragma: no cover - live_vm
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted workspace
        ["make", "-C", str(workspace)], check=False
    ).returncode


def _real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux``'s GNU build-id via the tested note parser.

    Dumps the ``.note.gnu.build-id`` section as raw bytes with ``objcopy`` and feeds them
    to :func:`parse_gnu_build_id`, so the shipped extraction is the unit-tested logic (not
    a locale-fragile ``readelf`` text scrape).
    """
    with tempfile.NamedTemporaryFile(suffix=".note") as note_file:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "objcopy",
                "-O",
                "binary",
                "--only-section=.note.gnu.build-id",
                str(workspace / "vmlinux"),
                note_file.name,
            ],
            check=True,
        )
        notes = Path(note_file.name).read_bytes()
    return parse_gnu_build_id(notes)


class _ValidatorStore(Protocol):
    def head(self, key: str) -> HeadResult | None: ...
    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...


class ValidatedUpload(NamedTuple):
    """Validation result: the recorded ``BuildOutput`` plus the per-name ``HeadResult``s.

    The heads (etag/size/checksum per uploaded object) are returned so the finalize step
    writes the write-once ``artifacts`` rows from this one validation pass — no second
    HEAD, and no second object-store handle — which keeps ``complete_build`` injectable.
    """

    output: BuildOutput
    heads: dict[str, HeadResult]


def _build_failure(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.BUILD_FAILURE, details=details)


def validate_external_artifacts(
    store: _ValidatorStore,
    *,
    manifest: Sequence[ManifestEntry],
    keys: Mapping[str, str],
    declared_build_id: str | None,
) -> ValidatedUpload:
    """Validate uploaded build artifacts; return the ``BuildOutput`` plus per-name heads.

    Order (ADR-0048 §5): require ``kernel``; then per declared artifact HEAD existence +
    size, checksum vs the manifest, and leading-byte magic; then, if a ``vmlinux`` is
    present, verify the declared ``build_id`` against its ranged ``.note.gnu.build-id``.

    ``declared_build_id`` is the GNU build-id as hex (the value ``parse_gnu_build_id``
    yields); the comparison is case-insensitive and the returned ``BuildOutput.build_id``
    is the normalized lowercase-hex value extracted from the file, not the raw input.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (a missing/skipped upload, or a vmlinux
            with no declared build_id); ``BUILD_FAILURE`` (checksum/size/magic/build_id
            defect); ``INFRASTRUCTURE_FAILURE`` propagated from the store.
    """
    by_name = {e.name: e for e in manifest}
    if "kernel" not in by_name or "kernel" not in keys:
        raise CategorizedError(
            "external build is missing the required kernel artifact",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    heads: dict[str, HeadResult] = {}
    for name, entry in by_name.items():
        key = keys[name]
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
        heads[name] = head

    build_id = ""
    if "vmlinux" in by_name:
        if not declared_build_id:
            raise CategorizedError(
                "a vmlinux upload requires a declared build_id",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        actual = extract_build_id_ranged(store, keys["vmlinux"])  # lowercase hex
        if actual != declared_build_id.lower():
            raise _build_failure("declared build_id does not match the uploaded vmlinux")
        build_id = actual

    output = BuildOutput(
        kernel_ref=keys["kernel"],
        debuginfo_ref=keys.get("vmlinux", ""),
        build_id=build_id,
    )
    return ValidatedUpload(output=output, heads=heads)


def _check_magic(store: _ValidatorStore, name: str, key: str) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        magic = store.get_range(key, start=_BZIMAGE_MAGIC_OFFSET, length=4)
        if magic != _BZIMAGE_MAGIC:
            raise _build_failure("kernel is not a bzImage", name=name)
    # initrd has no cheap universal magic; checksum + size already gate it.


def extract_build_id_ranged(store: _ValidatorStore, key: str) -> str:
    """Extract a vmlinux's GNU build-id via ranged ELF64-LE reads (no full download).

    Reads the ELF header (``e_shoff``/``e_shentsize``/``e_shnum``/``e_shstrndx``), the
    section header table, the section-name string table, and the ``.note.gnu.build-id``
    section bytes — then feeds them to :func:`parse_gnu_build_id`.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if the ELF is malformed or carries no build-id.
    """
    header = store.get_range(key, start=0, length=64)
    if header[:4] != _ELF_MAGIC or header[4] != 2 or header[5] != 1:  # ELFCLASS64, ELFDATA2LSB
        raise _build_failure("vmlinux is not a 64-bit little-endian ELF")
    e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", header, 0x3E)[0]
    if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64:
        raise _build_failure("vmlinux has no usable section header table")
    sht = store.get_range(key, start=e_shoff, length=e_shentsize * e_shnum)
    shstr = _read_section(store, key, sht, e_shentsize, e_shstrndx)
    for i in range(e_shnum):
        off = i * e_shentsize
        sh_name = struct.unpack_from("<I", sht, off)[0]
        sh_type = struct.unpack_from("<I", sht, off + 4)[0]
        if sh_type != _SHT_NOTE:
            continue
        name = shstr[sh_name : shstr.index(b"\x00", sh_name)]
        if name == b".note.gnu.build-id":
            notes = _read_section(store, key, sht, e_shentsize, i)
            return parse_gnu_build_id(notes)
    raise _build_failure("vmlinux carries no .note.gnu.build-id section")


def _read_section(
    store: _ValidatorStore, key: str, sht: bytes, e_shentsize: int, index: int
) -> bytes:
    off = index * e_shentsize
    sh_offset = struct.unpack_from("<Q", sht, off + 0x18)[0]
    sh_size = struct.unpack_from("<Q", sht, off + 0x20)[0]
    return store.get_range(key, start=sh_offset, length=sh_size)


__all__ = [
    "BuildOutput",
    "Builder",
    "LocalLibvirtBuild",
    "ValidatedUpload",
    "extract_build_id_ranged",
    "parse_gnu_build_id",
    "validate_external_artifacts",
]
