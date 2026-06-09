"""Local-libvirt Install + boot plane: stage a direct-kernel boot, bring the System up (ADR-0030).

`LocalLibvirtInstall` realizes two handler-facing ports keyed on the System-tagged libvirt
domain (`kdive-{system_id}`, minted by the provisioning plane, ADR-0025):

- `install(request)` stages the kernel
  (and optionally an initrd) to a **per-Run** host-local path
  (`{staging_root}/{system_id}/{run_id}/{kernel[,initrd]}`) via a temp-then-rename fetch.
  The kdump capture prerequisite check fires only for `method=CaptureMethod.KDUMP`; non-kdump
  boots skip it. When `initrd_ref` is ``None`` (e.g. a bzImage with embedded initramfs) no
  initrd is fetched and no `<initrd>` element is emitted. `defineXML`s the domain with a
  direct-kernel `<os>` (`<kernel>`/[`<initrd>`]/`<cmdline>`). The `<os>` is built with
  `xml.etree.ElementTree` (no string interpolation), so a `cmdline` value cannot inject XML.
- `boot(system_id)` power-cycles the domain into the staged `<kernel>` (`destroy` if running,
  then `create`) and polls the run-readiness preflight within a bounded window: the System
  never answering is `boot_timeout`; answering-but-failing a check is `readiness_failure`; a
  libvirt error starting the domain is `install_failure`.

DB-free: it owns no Postgres — the `runs.*` install/boot handlers drive the step ledger.
The slow, host-bound seams (libvirt connect, object-store fetch, kdump/readiness checks, the
poll clock) are **injected**, so unit tests cover the orchestration/error contract without a
host; the real `libvirt.open`/object-store path is `live_vm`-only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess  # noqa: S404 - virsh domstate is invoked with a fixed argv, no shell
import time
import xml.etree.ElementTree as ET  # noqa: S405 - constructs/edits self-owned domain XML only
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple, Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.constants import (
    DEFAULT_LIBVIRT_URI,
    LIBVIRT_URI_ENV,
)
from kdive.providers.ports import InstallRequest
from kdive.providers.runtime_paths import console_log_path, domain_name_for, read_console_log
from kdive.store.objectstore import FetchedArtifact, object_store_from_env

_log = logging.getLogger(__name__)

_STAGING_ENV = "KDIVE_INSTALL_STAGING"
_DEFAULT_STAGING = "/var/lib/kdive/install"
_DEFAULT_BOOT_WINDOW_POLLS = 30
# The boot window is _DEFAULT_BOOT_WINDOW_POLLS × _POLL_INTERVAL_SECONDS = 150s (ADR-0055 §7):
# boot()._await_ready loops the poll count; _real_readiness owns the per-poll cadence.
_POLL_INTERVAL_SECONDS = 5.0
_DOMSTATE_PROBE_TIMEOUT = 10
_TERMINAL_DOMSTATES = frozenset({"shut off", "crashed"})

_READINESS_MARKER = "kdive-ready"
# Fatal/stall-grade kernel crash signatures (ADR-0055 §4). Fail-closed and additive.
# The lookbehinds keep `BUG:`/`Oops:` from matching benign substrings (e.g. `DEBUG:`).
_CRASH_SIGNATURE = re.compile(
    r"Kernel panic"
    r"|(?<![A-Za-z])BUG:"
    r"|(?<![A-Za-z])Oops:"
    r"|general protection fault"
    r"|[Uu]nable to handle kernel"
    r"|KASAN:"
    r"|KFENCE:"
    r"|detected stall"
)

ConsoleVerdict = Literal["ready", "crashed", "pending"]


class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool
    probe_error: str | None = None


class _DomainExitProbe(NamedTuple):
    """The domstate probe result plus a bounded probe-failure diagnostic."""

    exited: bool
    error: str | None = None


class _LibvirtDomain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name
    def isActive(self) -> int: ...  # noqa: N802 - mirrors the libvirt binding name
    def create(self) -> int: ...
    def destroy(self) -> int: ...


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def defineXML(self, xml: str) -> _LibvirtDomain: ...  # noqa: N802 - libvirt name
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]
type Fetch = Callable[[str, Path], None]
type Readiness = Callable[[UUID], ReadinessResult]


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtInstall:
    """The realized `Installer` + `Booter` for the local libvirt host (ADR-0030)."""

    def __init__(
        self,
        *,
        connect: Connect,
        fetch_kernel: Fetch,
        fetch_initrd: Fetch,
        readiness: Readiness,
        staging_root: Path,
        boot_window_polls: int = _DEFAULT_BOOT_WINDOW_POLLS,
    ) -> None:
        self._connect = connect
        self._fetch_kernel = fetch_kernel
        self._fetch_initrd = fetch_initrd
        self._readiness = readiness
        self._staging_root = staging_root
        self._boot_window_polls = boot_window_polls

    @classmethod
    def from_env(cls) -> LocalLibvirtInstall:
        """Build from the ``KDIVE_*`` environment; does not connect to libvirt or the store.

        The fetch seam is the real object-store read (`_real_fetch` → `_stage_object`,
        ADR-0054): it builds the store lazily from the ``KDIVE_S3_*`` env on the first call,
        so the worker registers its handlers without S3 env present, and the network I/O runs
        only when an install fetches. The real readiness preflight (`_real_readiness`) tails the
        teed console under the `live_vm` gate (it needs a running host); the kdump prerequisite
        is a host-observable initrd-presence check inside ``install`` (ADR-0055 §5), not a seam.
        """
        host_uri = os.environ.get(LIBVIRT_URI_ENV, DEFAULT_LIBVIRT_URI)
        staging_root = Path(os.environ.get(_STAGING_ENV, _DEFAULT_STAGING))
        return cls(
            connect=lambda: libvirt.open(host_uri),
            fetch_kernel=_real_fetch,
            fetch_initrd=_real_fetch,
            readiness=_real_readiness,
            staging_root=staging_root,
        )

    def install(self, request: InstallRequest) -> None:
        """Stage the kernel (and optionally initrd) and redefine the domain for direct-kernel boot.

        The initrd fetch and ``<initrd>`` element are omitted when ``initrd_ref`` is ``None``
        (e.g. a bzImage with an embedded initramfs). The kdump preflight is gated on
        ``method == CaptureMethod.KDUMP`` — non-kdump boots do not require kdump prerequisites.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the kdump capture path is absent
                (method=kdump only, checked before any redefine); ``INSTALL_FAILURE`` on a
                libvirt redefine error; ``INFRASTRUCTURE_FAILURE`` if the per-Run staging
                directory cannot be created; any fetch error category propagated from the seam.
        """
        staging_dir = self._staging_root / str(request.system_id) / str(request.run_id)
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CategorizedError(
                "failed to create the per-Run staging directory",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"op": "mkdir", "dest": str(staging_dir)},
            ) from exc
        kernel_path = staging_dir / "kernel"
        self._fetch_kernel(request.kernel_ref, kernel_path)
        initrd_path: Path | None = None
        if request.initrd_ref is not None:
            initrd_path = staging_dir / "initrd"
            self._fetch_initrd(request.initrd_ref, initrd_path)
        if request.method is CaptureMethod.KDUMP and not _kdump_capture_present(initrd_path):
            raise CategorizedError(
                "kdump capture initramfs not staged (a separate initrd is required for kdump)",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(request.system_id)},
            )
        domain_name = domain_name_for(request.system_id)
        conn = self._open("for install")
        try:
            xml = self._render_direct_kernel_xml(
                conn, domain_name, kernel_path, initrd_path, request.cmdline
            )
            try:
                conn.defineXML(xml)
            except libvirt.libvirtError as exc:
                raise self._install_failure("redefining", domain_name) from exc
        finally:
            _close(conn)

    def boot(self, system_id: UUID) -> None:
        """Power-cycle the domain into the staged kernel and confirm run-readiness.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` if the domain is absent or libvirt cannot
                start it; ``BOOT_TIMEOUT`` if the System never answers within the boot window;
                ``READINESS_FAILURE`` if it answers but a readiness check fails.
        """
        domain_name = domain_name_for(system_id)
        conn = self._open("to boot")
        try:
            domain = self._lookup(conn, domain_name)
            self._power_cycle(domain, domain_name)
        finally:
            _close(conn)
        self._await_ready(system_id)

    def _render_direct_kernel_xml(
        self,
        conn: _LibvirtConn,
        domain_name: str,
        kernel_path: Path,
        initrd_path: Path | None,
        cmdline: str,
    ) -> str:
        """Read the existing domain XML and add a direct-kernel `<os>` section (ADR-0030 §5).

        ``initrd_path`` is optional: when ``None`` (embedded-initramfs kernel) no ``<initrd>``
        element is emitted, so libvirt boots the kernel without a separate initrd.
        """
        try:
            domain = conn.lookupByName(domain_name)
            current = domain.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise self._install_failure("looking up", domain_name) from exc
        # `XMLDesc` crosses the same libvirtd trust boundary the discovery plane parses
        # with defusedxml: parse it the same way so a DOCTYPE/entity-expansion document
        # cannot become a billion-laughs DoS here. A malformed/forbidden document is a
        # clean install_failure, not a raw parser exception out of the handler.
        try:
            root = _safe_fromstring(current)
        except (ET.ParseError, DefusedXmlException) as exc:
            raise self._install_failure("parsing the domain XML of", domain_name) from exc
        os_el = root.find("os")
        if os_el is None:
            os_el = ET.SubElement(root, "os")
        for tag in ("kernel", "initrd", "cmdline"):
            existing = os_el.find(tag)
            if existing is not None:
                os_el.remove(existing)
        ET.SubElement(os_el, "kernel").text = str(kernel_path)
        if initrd_path is not None:
            ET.SubElement(os_el, "initrd").text = str(initrd_path)
        ET.SubElement(os_el, "cmdline").text = cmdline
        return ET.tostring(root, encoding="unicode")

    def _power_cycle(self, domain: _LibvirtDomain, domain_name: str) -> None:
        try:
            if domain.isActive():
                domain.destroy()
            domain.create()
        except libvirt.libvirtError as exc:
            raise self._install_failure("power-cycling", domain_name) from exc

    def _await_ready(self, system_id: UUID) -> None:
        answered = False
        first_probe_error: str | None = None
        for _ in range(self._boot_window_polls):
            result = self._readiness(system_id)
            if first_probe_error is None and result.probe_error is not None:
                first_probe_error = result.probe_error
            if result.answered:
                answered = True
                if result.ok:
                    return
                details: dict[str, object] = {"system_id": str(system_id)}
                if first_probe_error is not None:
                    details["probe_error"] = first_probe_error
                raise CategorizedError(
                    "System booted but a run-readiness check failed",
                    category=ErrorCategory.READINESS_FAILURE,
                    details=details,
                )
        category = ErrorCategory.READINESS_FAILURE if answered else ErrorCategory.BOOT_TIMEOUT
        details: dict[str, object] = {"system_id": str(system_id)}
        if first_probe_error is not None:
            details["probe_error"] = first_probe_error
        raise CategorizedError(
            "System did not become ready within the boot window",
            category=category,
            details=details,
        )

    def _open(self, purpose: str) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._install_failure(f"connecting to libvirt {purpose}", "install") from exc

    @staticmethod
    def _lookup(conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise LocalLibvirtInstall._install_failure("looking up", domain_name) from exc

    @staticmethod
    def _install_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INSTALL_FAILURE,
            details={"domain": domain_name},
        )


def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
    """Classify a console capture: did the System reach the marker, crash, or neither?

    The marker is matched as a whole line — the readiness unit echoes the bare line
    ``kdive-ready`` to the console, while systemd's ``Starting kdive-ready.service`` line
    (same substring) is not the signal (ADR-0055 §3). A crash signature (§4) in the
    pre-marker region wins (crash-wins, fail-closed). Bytes are decoded utf-8 with
    ``errors="replace"`` so a partial multibyte tail or non-UTF-8 console never raises.

    Returns:
        ``"crashed"`` if a crash signature precedes the marker (or the marker is absent),
        ``"ready"`` if a bare marker line is present with no crash before it, else
        ``"pending"``.
    """
    text = data.decode("utf-8", errors="replace")
    marker_re = re.compile(rf"^[^\S\n]*{re.escape(marker)}[^\S\n]*$", re.MULTILINE)
    marker_match = marker_re.search(text)
    region = text if marker_match is None else text[: marker_match.start()]
    if _CRASH_SIGNATURE.search(region):
        return "crashed"
    return "ready" if marker_match is not None else "pending"


class _ObjectReader(Protocol):
    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact: ...


def _stage_object(store: _ObjectReader, ref: str, dest: Path) -> None:
    """Read object ``ref`` from the store and write it to ``dest`` via temp-then-rename.

    ``ref`` is a key the system itself produced (the Run's ``kernel_ref``/``initrd_ref``),
    so the read is **unconditional** (``etag=None``, ADR-0054) — the install plane holds no
    client handle to validate. The bytes are written to a sibling ``.part`` file and
    atomically renamed into ``dest``, so a failure partway leaves ``dest`` untouched and no
    partial file the redefine could point at.

    Raises:
        CategorizedError: a store fault — ``STALE_HANDLE`` for a vanished key,
            ``INFRASTRUCTURE_FAILURE`` otherwise (from ``get_artifact``); or a local
            staging-write fault (disk full, permission), mapped to
            ``INFRASTRUCTURE_FAILURE`` with the destination path so the failure is not an
            opaque ``OSError`` out of the seam.
    """
    data = store.get_artifact(ref, None).data
    _write_staged_bytes(dest, data)


def _write_staged_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` through a sibling temp file, then atomically replace ``dest``."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
        tmp.replace(dest)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()  # best-effort: drop any partial temp; never mask the real error
        raise CategorizedError(
            "failed to write the staged object to the per-Run path",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "stage", "dest": str(dest)},
        ) from exc


def _real_fetch(ref: str, dest: Path) -> None:  # pragma: no cover - live_vm
    _stage_object(object_store_from_env(), ref, dest)


def _kdump_capture_present(initrd_path: Path | None) -> bool:
    """Host-observable kdump prerequisite: a separate capture initramfs was staged.

    A ``crashkernel=`` reservation is inert without a capture initramfs (ADR-0030 §4).
    This is necessary, not sufficient — it does not prove the initrd is kdump-capable;
    the in-guest verification lands with #115 (ADR-0055 §5). An embedded-initramfs kernel
    (``initrd_ref=None`` → ``initrd_path is None``) is rejected for kdump (the M0 boundary).
    """
    return initrd_path is not None and initrd_path.exists()


def _bounded_probe_error(message: str) -> str:
    return message[:200]


def _domain_exit_probe(domain_name: str) -> _DomainExitProbe:  # pragma: no cover - live_vm
    """Return whether ``virsh domstate`` reports terminal state plus probe diagnostics.

    A probe error/timeout or a transient non-running state (``paused``, ``in shutdown``)
    is not proof of exit (v1: a flaky/slow probe keeps waiting), so ``exited`` is
    ``False`` and the caller keeps polling (ADR-0055 §7). Probe failures keep a bounded
    diagnostic so a final boot timeout can distinguish a silent guest from a broken host
    probe.
    """
    uri = os.environ.get(LIBVIRT_URI_ENV, DEFAULT_LIBVIRT_URI)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["virsh", "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _DomainExitProbe(
            False,
            f"virsh domstate timed out after {exc.timeout:g}s",
        )
    except FileNotFoundError:
        return _DomainExitProbe(False, "virsh executable not found")
    except (subprocess.SubprocessError, OSError) as exc:
        return _DomainExitProbe(False, _bounded_probe_error(f"virsh domstate probe failed: {exc}"))
    if proc.stdout.strip().lower() in _TERMINAL_DOMSTATES:
        return _DomainExitProbe(True)
    stderr = proc.stderr.strip().lower()
    exited = (
        proc.returncode != 0
        and domain_name.startswith("kdive-")
        and "failed to get domain" in stderr
    )
    if exited:
        return _DomainExitProbe(True)
    if proc.returncode != 0:
        error = stderr or f"virsh domstate exited {proc.returncode}"
        return _DomainExitProbe(False, _bounded_probe_error(error))
    return _DomainExitProbe(False)


def _domain_exited(domain_name: str) -> bool:  # pragma: no cover - live_vm
    """True only if ``virsh domstate`` reports a terminal state (shut off / crashed)."""
    return _domain_exit_probe(domain_name).exited


def _verdict_to_result(verdict: ConsoleVerdict, *, exited: bool) -> ReadinessResult | None:
    """Map a console verdict (+ domain-exited flag) to a readiness result, or ``None``.

    Pure (host-free, the unit-tested core of the live probe, ADR-0055 §6/§7):

    - ``ready`` → answered + ok (the marker line was reached).
    - ``crashed`` → answered + not ok (a pre-marker crash signature — the demo's failure signal).
    - ``pending`` with the guest **exited** → answered + not ok (v1's ``exited``: it stopped
      without reaching the marker).
    - ``pending`` with the guest still running → ``None``, meaning "no answer yet, keep polling".
    """
    if verdict == "ready":
        return ReadinessResult(answered=True, ok=True)
    if verdict == "crashed":
        return ReadinessResult(answered=True, ok=False)
    if exited:
        return ReadinessResult(answered=True, ok=False)
    return None


def _real_readiness(system_id: UUID) -> ReadinessResult:  # pragma: no cover - live_vm
    """One run-readiness probe of the System's truncated console (ADR-0055 §6/§7).

    A single per-poll probe — ``boot()._await_ready`` drives the repetition. Reads the
    console, classifies it (`classify_console`), and maps the verdict (`_verdict_to_result`).
    On a ``pending`` verdict it re-reads once after a `virsh domstate` exit check so a marker
    or crash that landed just before the guest stopped is honored; a still-running guest
    sleeps one poll interval and stays unanswered, so the boot window (poll count × interval)
    elapses as ``boot_timeout`` if the System never comes up.
    """
    log_path = console_log_path(system_id)
    result = _verdict_to_result(classify_console(read_console_log(log_path)), exited=False)
    if result is not None:
        return result
    probe = _domain_exit_probe(domain_name_for(system_id))
    if probe.exited:
        return _verdict_to_result(
            classify_console(read_console_log(log_path)), exited=True
        ) or ReadinessResult(answered=True, ok=False)
    time.sleep(_POLL_INTERVAL_SECONDS)
    return ReadinessResult(answered=False, ok=False, probe_error=probe.error)


__all__ = [
    "LocalLibvirtInstall",
    "ReadinessResult",
    "classify_console",
    "read_console_log",
]
