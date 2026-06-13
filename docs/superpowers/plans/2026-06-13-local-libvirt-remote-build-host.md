# Local-libvirt builds on a remote build host — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or
> superpowers:subagent-driven-development to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `local_libvirt` provider build on an `ssh`/`ephemeral_libvirt` build host by
giving its builder a transport-bound path and switching the BUILD handler to capability-based
dispatch.

**Architecture:** Extract the provider-neutral artifact-publish helper (`ArtifactSource` +
presigned PUT) into the shared `build_host` layer; make `LocalLibvirtBuild` produce
`ArtifactSource`s and gain `over_transport`; replace the handler's `RemoteLibvirtBuild`
type-narrowing with a `TransportCapableBuilder` protocol check. No schema/selection/lease change.

**Tech Stack:** Python 3.13, `uv`, `pytest`, `ruff`, `ty`. Spec:
`docs/superpowers/specs/2026-06-13-local-libvirt-remote-build-host.md`. ADR:
`docs/adr/0101-local-libvirt-remote-build-host.md`.

**Guardrails (run before every commit):** `just lint` · `just type` · the focused tests for the
files touched, then `just test` before the final commit. CI runs `lint`/`type`/`test` recipes
individually, so each must be green locally.

---

### Task 1: Extract the neutral artifact-publish helper into `build_host`

**Files:**
- Create: `src/kdive/providers/build_host/artifact_publish.py`
- Create: `tests/providers/build_host/test_artifact_publish.py`

Move `ArtifactBytes`/`ArtifactRemoteFile`/`ArtifactSource`, a `StorePort` protocol, and the
presigned-publish helper (`publish_artifact_source` + the host `sha256sum`/`stat` readers + the
capped-TTL helper) out of `remote_libvirt/build.py` into a neutral module, parameterized by
`tenant`/`sensitivity`/`retention_class`. The logic is copied verbatim from
`remote_libvirt/build.py` lines 86-107, 281-404 — only the hard-coded `_TENANT`/`_SENSITIVITY`/
`_RETENTION_CLASS` become parameters and `_StorePort` becomes public `StorePort`.

- [ ] **Step 1: Write the failing tests** (`tests/providers/build_host/test_artifact_publish.py`)

```python
"""Provider-neutral artifact publishing (ADR-0099/0101)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
)
from kdive.providers.build_host.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    publish_artifact_source,
)
from kdive.providers.build_host.transport import CommandResult

_RUN = UUID("33333333-3333-3333-3333-333333333333")


@dataclass
class _FakeStore:
    puts: list[ArtifactWriteRequest] = field(default_factory=list)
    presigns: list[PresignPutRequest] = field(default_factory=list)

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.puts.append(request)
        return StoredArtifact(
            request.key(), "etag-" + request.name, request.sensitivity, request.retention_class
        )

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        self.presigns.append(request)
        return PresignedUpload(
            url=f"https://s3.example/{request.key}",
            required_headers={"x-amz-checksum-sha256": request.sha256},
        )


@dataclass
class _FakeTransport:
    files: dict[str, bytes] = field(default_factory=dict)
    uploaded: list[str] = field(default_factory=list)

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        if argv[0] == "sha256sum":
            digest = hashlib.sha256(self.files[argv[1]]).hexdigest()
            return CommandResult(returncode=0, stdout=f"{digest}  {argv[1]}\n", stderr="")
        if argv[0] == "stat":
            return CommandResult(returncode=0, stdout=f"{len(self.files[argv[-1]])}\n", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        self.uploaded.append(path)
        return f'"etag-{Path(path).name}"'.strip('"')

    # unused protocol members — real bodies (the codebase has no `...`-body concrete methods;
    # the strict whole-tree `ty` gate is happier with real returns + pragma).
    def read_text(self, path: str) -> str:  # pragma: no cover - unused
        return ""

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover - unused
        return b""

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover - unused
        return None

    def clone(self, remote: str, ref: str, dest: str) -> None:  # pragma: no cover - unused
        return None

    def cleanup(self, path: str) -> None:  # pragma: no cover - unused
        return None


def test_bytes_source_puts_with_tenant_owner_sensitivity() -> None:
    store = _FakeStore()
    stored = publish_artifact_source(
        store, _RUN, "kernel", ArtifactBytes(b"img"),
        tenant="local", sensitivity=Sensitivity.SENSITIVE, retention_class="build",
    )
    assert store.presigns == []
    [req] = store.puts
    assert (req.tenant, req.owner_kind, req.owner_id, req.name) == (
        "local", "runs", str(_RUN), "kernel"
    )
    assert req.data == b"img"
    assert req.sensitivity is Sensitivity.SENSITIVE
    assert req.retention_class == "build"
    assert stored.key == f"local/runs/{_RUN}/kernel"


def test_remote_file_presigns_base64_sha256_and_uploads() -> None:
    store, transport = _FakeStore(), _FakeTransport()
    content = b"\x1f\x8bbundle"
    path = "/build/kdive-bundle.tar.gz"
    transport.files[path] = content

    stored = publish_artifact_source(
        store, _RUN, "kernel", ArtifactRemoteFile(path=path, transport=transport),
        tenant="remote-libvirt", sensitivity=Sensitivity.SENSITIVE, retention_class="build",
    )

    assert store.puts == []
    [presign] = store.presigns
    assert presign.key == f"remote-libvirt/runs/{_RUN}/kernel"
    assert presign.sha256 == base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
    assert presign.size_bytes == len(content)
    assert transport.uploaded == [path]
    assert stored.key == presign.key


def test_remote_file_sha256sum_nonzero_is_build_failure() -> None:
    @dataclass
    class _FailHash(_FakeTransport):
        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
            if argv[0] == "sha256sum":
                return CommandResult(returncode=1, stdout="", stderr="No such file")
            return super().run(argv, cwd=cwd, timeout_s=timeout_s)

    store, transport = _FakeStore(), _FailHash()
    transport.files["/p"] = b"x"
    with pytest.raises(CategorizedError) as caught:
        publish_artifact_source(
            store, _RUN, "kernel", ArtifactRemoteFile(path="/p", transport=transport),
            tenant="local", sensitivity=Sensitivity.SENSITIVE, retention_class="build",
        )
    assert caught.value.category is ErrorCategory.BUILD_FAILURE
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/build_host/test_artifact_publish.py -q`
Expected: FAIL — `ModuleNotFoundError: ...artifact_publish`.

- [ ] **Step 3: Create `src/kdive/providers/build_host/artifact_publish.py`**

Copy the bodies of `_publish_remote_file`, `_remote_sha256_b64`, `_remote_size_bytes`,
`_presign_ttl_seconds` from `remote_libvirt/build.py` verbatim; thread `tenant`/`sensitivity`/
`retention_class` parameters into `_publish_remote_file` and `publish_artifact_source` (replacing
the module constants); rename `_StorePort` → public `StorePort`.

```python
"""Provider-neutral build-artifact publishing (ADR-0099/0101).

An artifact is either bytes the worker holds (PUT directly) or a file resident on a build
host (presigned PUT, hashed host-side so the worker never reads its bytes). Both build
providers build over the neutral ``build_host`` layer; this is its publish primitive
(ADR-0076 bars only provider<->provider coupling, not use of this shared layer).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

import kdive.config as config
from kdive.config.core_settings import UPLOAD_TTL_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.provider_components.artifacts import (
    ArtifactWriteRequest,
    PresignedUpload,
    PresignPutRequest,
    StoredArtifact,
    artifact_key,
)
from kdive.providers.build_host.transport import BuildTransport

_MAX_PRESIGN_TTL_S = 3600


@dataclass(slots=True, frozen=True)
class ArtifactBytes:
    """An artifact the worker holds in memory and publishes with a direct PUT."""

    data: bytes


@dataclass(slots=True, frozen=True)
class ArtifactRemoteFile:
    """An artifact that lives on a build host and publishes via a presigned PUT."""

    path: str
    transport: BuildTransport


type ArtifactSource = ArtifactBytes | ArtifactRemoteFile


class StorePort(Protocol):
    """The store surface the publish helper needs: a direct PUT and a presigned PUT."""

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload: ...


def publish_artifact_source(
    store: StorePort,
    run_id: UUID,
    name: str,
    source: ArtifactSource,
    *,
    tenant: str,
    sensitivity: Sensitivity,
    retention_class: str,
) -> StoredArtifact:
    """Publish one build artifact under ``<tenant>/runs/<run_id>/<name>`` and return its row.

    An :class:`ArtifactBytes` source is PUT directly from worker memory. An
    :class:`ArtifactRemoteFile` source is published via a presigned PUT whose checksum is
    computed on the build host, so the worker never reads the file's bytes.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` from a failed store/upload;
            ``BUILD_FAILURE`` if the host-side hash/size of a remote file cannot be read.
    """
    match source:
        case ArtifactBytes(data=data):
            return store.put_artifact(
                ArtifactWriteRequest(
                    tenant=tenant,
                    owner_kind="runs",
                    owner_id=str(run_id),
                    name=name,
                    data=data,
                    sensitivity=sensitivity,
                    retention_class=retention_class,
                )
            )
        case ArtifactRemoteFile(path=path, transport=transport):
            return _publish_remote_file(
                store,
                run_id,
                name,
                path,
                transport,
                tenant=tenant,
                sensitivity=sensitivity,
                retention_class=retention_class,
            )


def _publish_remote_file(
    store: StorePort,
    run_id: UUID,
    name: str,
    path: str,
    transport: BuildTransport,
    *,
    tenant: str,
    sensitivity: Sensitivity,
    retention_class: str,
) -> StoredArtifact:
    sha256_b64 = _remote_sha256_b64(transport, path)
    size_bytes = _remote_size_bytes(transport, path)
    key = artifact_key(tenant, "runs", str(run_id), name)
    presigned = store.presign_put(
        PresignPutRequest(
            key=key,
            sha256=sha256_b64,
            size_bytes=size_bytes,
            sensitivity=sensitivity,
            retention_class=retention_class,
            expires_in=_presign_ttl_seconds(),
        )
    )
    etag = transport.upload_file(path, presigned)
    return StoredArtifact(key, etag, sensitivity, retention_class)
```

Then paste `_remote_sha256_b64`, `_remote_size_bytes`, `_presign_ttl_seconds` **verbatim** from
`remote_libvirt/build.py` (lines 341-403). Add `__all__ = ["ArtifactBytes", "ArtifactRemoteFile",
"ArtifactSource", "StorePort", "publish_artifact_source"]`.

- [ ] **Step 4: Run to verify pass + guardrails**

Run: `uv run python -m pytest tests/providers/build_host/test_artifact_publish.py -q && just lint && just type`
Expected: tests PASS, lint/type clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/build_host/artifact_publish.py tests/providers/build_host/test_artifact_publish.py
git commit -m "feat(build): neutral artifact-publish helper in build_host"
```

---

### Task 2: Refactor `RemoteLibvirtBuild` onto the neutral helper

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/build.py`

Replace remote's private `ArtifactBytes`/`ArtifactRemoteFile`/`ArtifactSource`/`_StorePort`/
`_publish_remote_file`/`_remote_sha256_b64`/`_remote_size_bytes`/`_presign_ttl_seconds` with
imports from the neutral module (replace, no shim — CLAUDE.md). Keep the names importable from
`remote_libvirt.build` (it still constructs them in its seams) via the import + `__all__`.

- [ ] **Step 1: Edit imports** — add to `remote_libvirt/build.py`:

```python
from kdive.providers.build_host.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
    StorePort,
    publish_artifact_source,
)
```

Delete the now-unused imports (`base64`, `ArtifactWriteRequest`, `PresignPutRequest`,
`PresignedUpload`, `artifact_key`, `UPLOAD_TTL_SECONDS`) — let `ruff`/`ty` confirm which remain.

- [ ] **Step 2: Delete the moved definitions** — remove the `ArtifactBytes`/`ArtifactRemoteFile`
dataclasses (lines 86-104), the `type ArtifactSource = ...` (106), the `_StorePort` protocol
(109-113), `_MAX_PRESIGN_TTL_S` (80), `_publish_remote_file` (315-338), `_remote_sha256_b64`
(341-374), `_remote_size_bytes` (377-398), `_presign_ttl_seconds` (401-403).

- [ ] **Step 3: Rewrite `RemoteLibvirtBuild.publish` and store typing**

Replace the `publish` body's `match` with a delegation, and retype `store_factory`/`_store`/
`_store_for_publish` from `_StorePort` to `StorePort`:

```python
    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact under ``runs/<run_id>/<name>`` and return its row."""
        return publish_artifact_source(
            self._store_for_publish(),
            run_id,
            name,
            source,
            tenant=_TENANT,
            sensitivity=_SENSITIVITY,
            retention_class=_RETENTION_CLASS,
        )
```

- [ ] **Step 4: Delete the dead `build_over_transport` free function** (lines 531-551) — the
handler will call `over_transport` through its own bind seam (Task 4). Remove it from `__all__`
if present (it is not). Keep `over_transport` (the method) and `transport_make_bundle`/
`transport_vmlinux_source`.

- [ ] **Step 5: Run remote tests + guardrails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/test_build.py tests/providers/remote_libvirt/test_build_transport.py -q && just lint && just type`
Expected: PASS (the re-export keeps `from ...remote_libvirt.build import ArtifactBytes,
ArtifactRemoteFile` working). `ty` flags `build_over_transport` removal if anything still imports
it — Task 4 fixes the handler import; until then `just type` may flag `jobs/handlers/runs.py`.
Do Task 4 before the type gate must be green; commit this task together with Task 4 if needed.

- [ ] **Step 6: Commit (may be deferred to fold with Task 4)**

```bash
git add src/kdive/providers/remote_libvirt/build.py
git commit -m "refactor(build): remote-libvirt publish uses neutral helper"
```

---

### Task 3: Make `LocalLibvirtBuild` transport-capable

**Files:**
- Modify: `src/kdive/providers/local_libvirt/build.py`
- Modify: `tests/providers/local_libvirt/test_build.py`

- [ ] **Step 1: Update local test seams to `ArtifactSource`** in `test_build.py`:

In `_Seams` replace `read_kernel_image`/`read_vmlinux` with:

```python
    def read_kernel_source(self, workspace: Path) -> ArtifactSource:
        return ArtifactBytes(b"bzImage-bytes")

    def read_vmlinux_source(self, workspace: Path) -> ArtifactSource:
        return ArtifactBytes(b"vmlinux-bytes")
```

Update `_builder` and the three direct `LocalLibvirtBuild(...)` constructions (helper at ~147,
the missing-bzimage test ~316, the validate-config test ~393, the live_vm test ~555) to pass
`read_kernel_source=`/`read_vmlinux_source=` instead of `read_kernel_image=`/`read_vmlinux=`.
For the missing-bzimage test (~316) the seam must exercise the real reader:
`read_kernel_source=lambda ws: ArtifactBytes(build_host_execution.real_read_kernel_image(ws))`.
For the live_vm test (~555) likewise wrap both real readers in `ArtifactBytes(...)`.

Add imports: `from kdive.providers.build_host.artifact_publish import ArtifactBytes,
ArtifactSource`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q`
Expected: FAIL — `LocalLibvirtBuild.__init__` got unexpected `read_kernel_source`.

- [ ] **Step 3: Rewrite `LocalLibvirtBuild`** (`local_libvirt/build.py`):

Add imports:

```python
from kdive.providers.build_host.artifact_publish import (
    ArtifactBytes,
    ArtifactRemoteFile,
    ArtifactSource,
    StorePort,
    publish_artifact_source,
)
from kdive.providers.build_host.transport import BuildTransport
from kdive.providers.build_host.transport_seams import (
    transport_git_checkout,
    transport_read_build_id,
    transport_read_config,
    transport_run_make,
    transport_run_olddefconfig,
)
from kdive.security.secrets.secret_registry import SecretRegistry
```

Replace the `_StorePort` protocol and the two `ReadBytes` seam aliases with source-seam aliases:

```python
type _ReadArtifactSource = Callable[[Path], ArtifactSource]
```

Constructor: replace params `read_kernel_image`/`read_vmlinux` (typed `_build_exec.ReadBytes`)
with `read_kernel_source`/`read_vmlinux_source` (typed `_ReadArtifactSource`); type
`store_factory` as `Callable[[], StorePort]`, `_store` as `StorePort | None`; store
`self._catalog_fetch = catalog_fetch`. Drop the `_StorePort` class.

`build()` and `publish()`:

```python
    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        """Build a kernel and store two artifacts; return their refs and the build-id."""
        workspace = self._orchestrator.build_workspace(run_id, profile)
        build_id = self._read_build_id(workspace)
        kernel = self.publish(run_id, "kernel", self._read_kernel_source(workspace))
        vmlinux = self.publish(run_id, "vmlinux", self._read_vmlinux_source(workspace))
        return BuildOutput(kernel_ref=kernel.key, debuginfo_ref=vmlinux.key, build_id=build_id)

    def publish(self, run_id: UUID, name: str, source: ArtifactSource) -> StoredArtifact:
        """Publish one build artifact; bytes PUT directly, host files via presigned PUT."""
        if self._store is None:
            self._store = self._store_factory()
        return publish_artifact_source(
            self._store,
            run_id,
            name,
            source,
            tenant=self._tenant,
            sensitivity=Sensitivity.SENSITIVE,
            retention_class=_RETENTION_CLASS,
        )
```

Delete the old `_put`. Add `over_transport` (mirrors remote, no modules/bundle):

```python
    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> LocalLibvirtBuild:
        """Return a sibling builder whose build runs ON ``transport``'s host (ADR-0101).

        Every build step (git checkout, ``olddefconfig``, ``.config`` read, ``make``, build-id)
        runs over ``transport``; the bzImage and vmlinux are published from the host via
        presigned PUT. The worker-side config/store of ``self`` is reused. A local System
        direct-kernel-boots, so no modules bundle is produced (unlike remote-libvirt).
        """
        host_root = Path(host_workspace_root)
        return LocalLibvirtBuild(
            tenant=self._tenant,
            workspace_root=host_root,
            store_factory=self._store_factory,
            checkout=transport_git_checkout(transport, git_remote, git_ref, secret_registry),
            run_olddefconfig=transport_run_olddefconfig(transport),
            read_config=transport_read_config(transport),
            run_make=transport_run_make(transport),
            read_kernel_source=lambda ws: ArtifactRemoteFile(
                str(ws / "arch/x86/boot/bzImage"), transport
            ),
            read_vmlinux_source=lambda ws: ArtifactRemoteFile(str(ws / "vmlinux"), transport),
            read_build_id=transport_read_build_id(transport),
            secret_registry=secret_registry,
            catalog_fetch=self._catalog_fetch,
            allowed_component_roots=self._allowed_component_roots,
        )
```

Update `from_env` to pass the source seams (module-level helpers):

```python
def _local_kernel_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    return ArtifactBytes(_build_exec.real_read_kernel_image(workspace))


def _local_vmlinux_source(workspace: Path) -> ArtifactSource:  # pragma: no cover - live_vm
    return ArtifactBytes(_build_exec.real_read_vmlinux(workspace))
```

and in `from_env`: `read_kernel_source=_local_kernel_source, read_vmlinux_source=
_local_vmlinux_source, read_build_id=_build_exec.real_read_build_id,`.

- [ ] **Step 4: Add the `over_transport` unit test** to `test_build.py`:

```python
def test_over_transport_publishes_bzimage_and_vmlinux_via_presign(tmp_path: Path) -> None:
    # A transport-bound local build publishes the bare bzImage + vmlinux via presigned PUT
    # (no modules bundle), and the worker never reads the artifact bytes.
    import base64
    import hashlib

    from kdive.provider_components.artifacts import (
        PresignedUpload,
        PresignPutRequest,
        StoredArtifact as _SA,
    )
    from kdive.providers.build_host.transport import CommandResult

    @dataclass
    class _T:
        files: dict[str, bytes] = field(default_factory=dict)
        runs: list[list[str]] = field(default_factory=list)
        reads: list[str] = field(default_factory=list)
        uploaded: list[str] = field(default_factory=list)

        def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
            self.runs.append(argv)
            if argv[0] == "sha256sum":
                d = hashlib.sha256(self.files[argv[1]]).hexdigest()
                return CommandResult(0, f"{d}  {argv[1]}\n", "")
            if argv[0] == "stat":
                return CommandResult(0, f"{len(self.files[argv[-1]])}\n", "")
            if argv[0] == "objcopy":
                self.files[argv[-1]] = _gnu_build_id_note(b"\x01\x02\x03\x04")
                return CommandResult(0, "", "")
            return CommandResult(0, "", "")

        def read_text(self, path: str) -> str:
            return _GOOD_CONFIG

        def read_bytes(self, path: str) -> bytes:
            self.reads.append(path)
            return self.files[path]

        def write_bytes(self, path: str, data: bytes) -> None:
            self.files[path] = data

        def clone(self, remote: str, ref: str, dest: str) -> None:
            pass

        def upload_file(self, path: str, presigned: PresignedUpload) -> str:
            self.uploaded.append(path)
            return "etag-" + Path(path).name

        def cleanup(self, path: str) -> None:
            pass

    @dataclass
    class _Store:
        presigns: list[PresignPutRequest] = field(default_factory=list)
        puts: list[object] = field(default_factory=list)

        def put_artifact(self, request: ArtifactWriteRequest) -> _SA:
            self.puts.append(request)
            return _SA(request.key(), "e", request.sensitivity, request.retention_class)

        def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
            self.presigns.append(request)
            return PresignedUpload(url="https://s3/x", required_headers={})

    store, transport = _Store(), _T()
    ws = tmp_path / str(_RUN)
    transport.files[str(ws / "arch/x86/boot/bzImage")] = b"\x01bz"
    transport.files[str(ws / "vmlinux")] = b"\x7fELFvm"

    base = LocalLibvirtBuild(
        tenant=_TENANT,
        workspace_root=tmp_path / "warm",
        store_factory=lambda: store,
        checkout=lambda *_: None,
        run_olddefconfig=lambda _w: 0,
        read_config=lambda _w: _GOOD_CONFIG,
        run_make=lambda _w: 0,
        read_kernel_source=lambda _w: ArtifactBytes(b"x"),
        read_vmlinux_source=lambda _w: ArtifactBytes(b"y"),
        read_build_id=lambda _w: "deadbeef",
        secret_registry=SecretRegistry(),
        catalog_fetch=lambda _n: _FRAGMENT_BYTES,
    )
    bound = base.over_transport(
        transport,
        host_workspace_root=str(tmp_path),
        git_remote="https://git.example/linux.git",
        git_ref="v6.9",
        secret_registry=SecretRegistry(),
    )
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": {"git": {"remote": "https://git.example/linux.git", "ref": "v6.9"}},
            "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    out = bound.build(_RUN, profile)

    assert out.kernel_ref == f"{_TENANT}/runs/{_RUN}/kernel"
    assert out.debuginfo_ref == f"{_TENANT}/runs/{_RUN}/vmlinux"
    assert store.puts == []  # remote path never PUTs from worker memory
    assert {p.key for p in store.presigns} == {out.kernel_ref, out.debuginfo_ref}
    assert set(transport.uploaded) == {
        str(ws / "arch/x86/boot/bzImage"),
        str(ws / "vmlinux"),
    }
    # worker only reads the small objcopy note, never the artifacts
    assert transport.reads == [str(ws / "vmlinux.note")]
    heads = [a[0] for a in transport.runs]
    assert "make" in heads and "objcopy" in heads
```

- [ ] **Step 5: Run local tests + guardrails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_build.py -q && just lint && just type`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt/build.py tests/providers/local_libvirt/test_build.py
git commit -m "feat(build): transport-capable LocalLibvirtBuild.over_transport"
```

---

### Task 4: Capability-based BUILD-handler dispatch

**Files:**
- Modify: `src/kdive/providers/ports/build.py`
- Modify: `src/kdive/providers/ports/__init__.py`
- Modify: `src/kdive/jobs/handlers/runs.py`
- Modify: `tests/jobs/handlers/test_build_handler_transport.py`

- [ ] **Step 1: Add `TransportCapableBuilder` to `providers/ports/build.py`**

```python
from typing import Protocol, runtime_checkable
from uuid import UUID

from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.build_host.transport import BuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry


@runtime_checkable
class TransportCapableBuilder(Builder, Protocol):
    """A :class:`Builder` that can rebind its build onto a remote :class:`BuildTransport`."""

    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> Builder:
        """Return a sibling builder whose build runs on ``transport``'s host."""
        ...
```

Export it from `providers/ports/__init__.py` (add `TransportCapableBuilder` to the import and
`__all__`).

- [ ] **Step 2: Run to verify the new handler test fails** (write the test first)

Add to `tests/jobs/handlers/test_build_handler_transport.py`:

```python
from kdive.providers.local_libvirt.build import LocalLibvirtBuild


def test_ssh_host_local_provider_builder_succeeds_releases_lease(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local-libvirt builder on an ssh host now builds (was NOT_IMPLEMENTED) and frees the lease."""
    transport_builder = _RecordingBuilder()
    monkeypatch.setattr(runs_handlers, "ssh_build_transport_from_host", _fake_from_host)
    monkeypatch.setattr(
        runs_handlers, "bind_over_transport", lambda builder, transport, **kw: transport_builder
    )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_run(pool, _GIT_PROFILE)
            host = await _seed_ssh_host(pool)
            await _acquire_lease(pool, host, run_id)
            job = await _enqueue(pool, run_id, str(host.id))
            async with pool.connection() as conn:
                await runs_handlers.build_handler(
                    conn,
                    job,
                    resolver=provider_resolver(
                        builder=LocalLibvirtBuild.from_env(secret_registry=SecretRegistry())
                    ),
                    secret_registry=SecretRegistry(),
                )
            assert transport_builder.calls == [UUID(run_id)]
            assert await _run_state(pool, run_id) == "succeeded"
            assert await _lease_count(pool, run_id) == 0

    asyncio.run(_run())
```

Run: `uv run python -m pytest "tests/jobs/handlers/test_build_handler_transport.py::test_ssh_host_local_provider_builder_succeeds_releases_lease" -q`
Expected: FAIL — `AttributeError: ...has no attribute 'bind_over_transport'` (and, before the
edit, NOT_IMPLEMENTED).

- [ ] **Step 3: Rewrite the handler dispatch** in `jobs/handlers/runs.py`:

Replace the import line 31 with imports of the protocol (drop `build_over_transport` and the
`RemoteLibvirtBuild` import):

```python
from kdive.providers.ports import Booter, Builder, InstallRequest, TransportCapableBuilder
```

Replace `_require_remote_builder` with:

```python
def _require_transport_capable(
    builder: Builder, host: BuildHost, run_id: UUID
) -> TransportCapableBuilder:
    """Narrow ``builder`` to a transport-capable builder, or fail closed.

    Both remote build-host kinds (ssh, ephemeral_libvirt) run a transport-bound build, so this
    is checked once before any host-side work (no VM/transport is created for a known-bad combo).

    Raises:
        CategorizedError: ``NOT_IMPLEMENTED`` when the run's runtime builder cannot rebind onto
            a transport (it lacks ``over_transport``).
    """
    if not isinstance(builder, TransportCapableBuilder):
        raise CategorizedError(
            "a remote build host requires a transport-capable builder",
            category=ErrorCategory.NOT_IMPLEMENTED,
            details={"run_id": str(run_id), "build_host": host.name},
        )
    return builder
```

Add the patchable bind seam (replacing the call to the deleted `build_over_transport`):

```python
# Patchable seam: tests substitute this to inject a fake bound builder without over_transport.
def bind_over_transport(
    builder: TransportCapableBuilder,
    transport: BuildTransport,
    *,
    host_workspace_root: str,
    git_remote: str,
    git_ref: str,
    secret_registry: SecretRegistry,
) -> Builder:
    """Rebind ``builder`` onto ``transport`` with the profile's git coordinates."""
    return builder.over_transport(
        transport,
        host_workspace_root=host_workspace_root,
        git_remote=git_remote,
        git_ref=git_ref,
        secret_registry=secret_registry,
    )
```

In `_bind_transport`, retype `builder: RemoteLibvirtBuild` → `TransportCapableBuilder` and call
`bind_over_transport(...)` instead of `build_over_transport(...)`. In `_run_build`, replace
`remote_builder = _require_remote_builder(builder, host, run_id)` with
`capable = _require_transport_capable(builder, host, run_id)` and pass `capable` to both
`_bind_transport` calls.

- [ ] **Step 4: Update the existing patch sites** — in
`tests/jobs/handlers/test_build_handler_transport.py` rename every
`monkeypatch.setattr(runs_handlers, "build_over_transport", ...)` to `"bind_over_transport"`
(4 occurrences: lines ~251, ~294, ~402, ~437).

- [ ] **Step 5: Run the handler suite + guardrails**

Run: `uv run python -m pytest tests/jobs/handlers/test_build_handler_transport.py -q && just lint && just type`
Expected: all PASS, including the two `non_remote_builder_not_implemented` tests (the
`_RecordingBuilder` fake has no `over_transport`, so `_require_transport_capable` raises
NOT_IMPLEMENTED).

- [ ] **Step 6: Commit (fold Task 2's commit here if it was deferred)**

```bash
git add src/kdive/providers/ports/build.py src/kdive/providers/ports/__init__.py \
        src/kdive/jobs/handlers/runs.py tests/jobs/handlers/test_build_handler_transport.py \
        src/kdive/providers/remote_libvirt/build.py
git commit -m "feat(build): capability-based BUILD-handler transport dispatch (#356)"
```

---

### Task 5: Full guardrails + branch review

- [ ] **Step 1: Whole-suite guardrails**

Run: `just lint && just type && just test`
Expected: all green (the db/handler tests need Docker; they skip cleanly if absent, but run
locally where available).

- [ ] **Step 2: Adversarial branch review** — run the review loop with `challenge_args:
--base main` and the branch-safety focus. Address every defensible finding; commit per fix.

- [ ] **Step 3: PR** — push and open against `main`, body describing the diff, ending
`Closes #356`. Drive to green CI + `MERGEABLE`.

---

## Self-review notes

- **Spec coverage:** Task 1 = neutral helper; Task 2 = remote refactor (re-export parity); Task 3
  = local `over_transport` + `ArtifactSource` unification + warm-path parity (existing tests);
  Task 4 = capability dispatch + ssh-local success test + NOT_IMPLEMENTED fallback. All spec
  sections covered.
- **Type consistency:** `publish_artifact_source(store, run_id, name, source, *, tenant,
  sensitivity, retention_class)` and `over_transport(transport, *, host_workspace_root,
  git_remote, git_ref, secret_registry)` are used identically in every task and match the
  `TransportCapableBuilder` protocol.
- **Ordering hazard:** Task 2 removes `build_over_transport`, which `jobs/handlers/runs.py` still
  imports until Task 4. `just type` will flag the handler between Task 2 and Task 4 — fold the
  Task 2 commit into Task 4 (noted in Task 2 Step 6) so no commit lands with a red type gate.
