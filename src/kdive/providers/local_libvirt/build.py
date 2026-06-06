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
import shutil
import struct
import subprocess  # noqa: S404 - make is invoked with a fixed argv, no shell
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from kdive.db.upload_manifest import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.profiles.build import ServerBuildProfile
from kdive.security.redaction import Redactor
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
# Only shstrtab and the .note.gnu.build-id section are ever ranged-read (debug sections
# never are), so a small per-section cap bounds an untrusted vmlinux's declared sh_size
# against a multi-GB read into the worker thread.
_MAX_SECTION_BYTES = 16 * 1024 * 1024
# Trailing chars of a redacted rsync/git-apply stderr placed in error details (bounded so a
# large/noisy failure log cannot bloat a persisted error record).
_STDERR_TAIL = 2000

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


def _real_checkout(kernel_src: str, profile: ServerBuildProfile, workspace: Path) -> None:
    """Materialize a warm per-Run workspace, stage the ``.config``, apply any patch.

    Steps run in order so the resetting rsync (sync) precedes config-staging and patch
    application; see ADR-0053 for the per-step failure contract. The rsync sync and the
    later ``make`` run only on a real build host (``live_vm``); this composition itself is
    unit-tested with the steps stubbed.
    """
    _sync_tree(kernel_src, workspace)
    _stage_config(profile.config_ref, workspace)
    if profile.patch_ref is not None:
        _apply_patch(profile.patch_ref, workspace)


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


def _ref_error(kind: str, message: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` for a bad ref; ``details`` names the field, never its value."""
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )


def _resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref (``config_ref``/``patch_ref``) to an existing local file.

    Accepts a ``file:///abs/path`` URL (empty authority) or a bare absolute path; rejects a
    non-local scheme, a ``file://`` URL with a host, a non-absolute path, or a path that is
    not an existing regular file. The submitted ref value is never echoed in the error.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (``details={"kind": kind}``) for any
            unsupported or unresolvable reference.
    """
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise _ref_error(kind, "config/patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise _ref_error(kind, "config/patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise _ref_error(kind, "config/patch ref must be an absolute path")
    if not path.is_file():
        raise _ref_error(kind, "config/patch ref does not resolve to a readable file")
    return path


def _stage_config(config_ref: str, workspace: Path) -> None:
    """Copy the resolved ``config_ref`` to ``workspace/.config`` (overwriting any existing one)."""
    source = _resolve_local_ref(config_ref, kind="config_ref")
    shutil.copyfile(source, workspace / ".config")


def _redacted_tail(text: str) -> str:
    """Redact known secrets/``key=value`` pairs, then return the trailing ``_STDERR_TAIL`` chars."""
    return Redactor().redact_text(text)[-_STDERR_TAIL:]


def _apply_patch(patch_ref: str, workspace: Path) -> None:
    """Apply the resolved ``patch_ref`` to the workspace tree with ``git apply -p1``.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the ref is unresolvable (via
            :func:`_resolve_local_ref`) or the patch does not apply (a redacted stderr tail
            is placed in ``details``); ``MISSING_DEPENDENCY`` if ``git`` is absent.
    """
    patch = _resolve_local_ref(patch_ref, kind="patch_ref")
    if shutil.which("git") is None:
        raise CategorizedError(
            "git is required to apply a build patch",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "apply", "-p1", str(patch)],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "patch_ref does not apply against the kernel tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": _redacted_tail(result.stderr)},
        )


def _sync_tree(kernel_src: str, workspace: Path) -> None:
    """Mirror the warm ``kernel_src`` tree into ``workspace`` with ``rsync -a --delete``.

    Creates ``workspace`` (and missing parents) first, since ``build()`` does not and rsync
    does not create missing parent directories.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``kernel_src`` is empty or not a
            directory; ``MISSING_DEPENDENCY`` if ``rsync`` is absent;
            ``INFRASTRUCTURE_FAILURE`` on a non-zero rsync exit (redacted stderr in details).
    """
    if not kernel_src or not Path(kernel_src).is_dir():
        raise CategorizedError(
            "KDIVE_KERNEL_SRC is not set to an existing kernel source tree",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if shutil.which("rsync") is None:
        raise CategorizedError(
            "rsync is required to materialize the warm kernel tree",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    workspace.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["rsync", "-a", "--delete", f"{kernel_src.rstrip('/')}/", f"{workspace}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "rsync failed to materialize the workspace tree",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": _redacted_tail(result.stderr)},
        )


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
        CategorizedError: ``CONFIGURATION_ERROR`` (a missing/skipped upload, an artifact
            with no upload key, or a vmlinux with no declared build_id); ``BUILD_FAILURE``
            (checksum/size/magic/build_id defect). Store exceptions propagate as raised
            (the production ObjectStore wraps them as ``INFRASTRUCTURE_FAILURE``).
    """
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
        # heads["vmlinux"].size_bytes is already verified equal to the manifest size and
        # capped (artifacts.create_upload), so it is a safe ceiling for the ranged reads.
        actual = extract_build_id_ranged(
            store, keys["vmlinux"], max_size=heads["vmlinux"].size_bytes
        )  # lowercase hex
        if actual != declared_build_id.lower():
            raise _build_failure("declared build_id does not match the uploaded vmlinux")
        build_id = actual

    output = BuildOutput(
        kernel_ref=keys["kernel"],
        debuginfo_ref=keys.get("vmlinux", ""),
        build_id=build_id,
    )
    return ValidatedUpload(output=output, heads=heads)


def _validate_one_artifact(
    store: _ValidatorStore, name: str, entry: ManifestEntry, key: str
) -> HeadResult:
    """HEAD existence + size/checksum vs the manifest + leading-byte magic; return the head."""
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


def _check_magic(store: _ValidatorStore, name: str, key: str) -> None:
    if name == "vmlinux":
        if store.get_range(key, start=0, length=4) != _ELF_MAGIC:
            raise _build_failure("vmlinux is not an ELF file", name=name)
    elif name == "kernel":
        magic = store.get_range(key, start=_BZIMAGE_MAGIC_OFFSET, length=4)
        if magic != _BZIMAGE_MAGIC:
            raise _build_failure("kernel is not a bzImage", name=name)
    # initrd has no cheap universal magic; checksum + size already gate it.


def extract_build_id_ranged(store: _ValidatorStore, key: str, *, max_size: int) -> str:
    """Extract a vmlinux's GNU build-id via ranged ELF64-LE reads (no full download).

    Reads the ELF header (``e_shoff``/``e_shentsize``/``e_shnum``/``e_shstrndx``), the
    section header table, the section-name string table, and the ``.note.gnu.build-id``
    section bytes — then feeds them to :func:`parse_gnu_build_id`.

    ``max_size`` is the verified object size; every ranged read is bounded against it (and
    a per-section cap) so a crafted ELF declaring an oversized section cannot force a
    multi-GB read.

    Raises:
        CategorizedError: ``BUILD_FAILURE`` if the ELF is malformed, declares a section
            past ``max_size``/the cap, or carries no build-id.
    """
    header = store.get_range(key, start=0, length=64)
    if len(header) < 64:
        raise _build_failure("vmlinux ELF header is truncated")
    if header[:4] != _ELF_MAGIC or header[4] != 2 or header[5] != 1:  # ELFCLASS64, ELFDATA2LSB
        raise _build_failure("vmlinux is not a 64-bit little-endian ELF")
    try:
        e_shoff = struct.unpack_from("<Q", header, 0x28)[0]
        e_shentsize = struct.unpack_from("<H", header, 0x3A)[0]
        e_shnum = struct.unpack_from("<H", header, 0x3C)[0]
        e_shstrndx = struct.unpack_from("<H", header, 0x3E)[0]
        if e_shoff == 0 or e_shnum == 0 or e_shentsize < 64:
            raise _build_failure("vmlinux has no usable section header table")
        # Cap the SHT read itself: e_shentsize*e_shnum is u16*u16 (~4 GiB worst case), so
        # without an absolute bound a crafted ~object-sized vmlinux could force a multi-GB
        # read here even though the per-object guard below passes. 16 MiB ~= 262k entries.
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


def _find_build_id_note(
    store: _ValidatorStore,
    key: str,
    sht: bytes,
    shstr: bytes,
    e_shentsize: int,
    e_shnum: int,
    *,
    max_size: int,
) -> str:
    """Walk the SHT for the ``.note.gnu.build-id`` SHT_NOTE section and parse its build-id."""
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
    store: _ValidatorStore, key: str, sht: bytes, e_shentsize: int, index: int, *, max_size: int
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


__all__ = [
    "BuildOutput",
    "Builder",
    "LocalLibvirtBuild",
    "ValidatedUpload",
    "extract_build_id_ranged",
    "parse_gnu_build_id",
    "validate_external_artifacts",
]
