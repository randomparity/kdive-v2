# M2 Remote-libvirt Foundation (issue #200) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land M2's serial foundation: the independent `remote_libvirt` provider package with a mutual-TLS `qemu+tls://` connection seam, discovery, `ResourceKind.REMOTE_LIBVIRT` + migration 0020, the `presign_get` object-store primitive, opt-in composition registration, and the per-PR CI portability gate.

**Architecture:** A new `src/kdive/providers/remote_libvirt/` package (no shared layer with `local_libvirt`, ADR-0076) satisfies the typed `ProviderRuntime` ports. The control transport is `qemu+tls://` with mutual TLS; the client cert/key/CA are secrets-by-reference, materialized per-op into a private pkipath and deleted on every exit path (ADR-0077). Planes that land in M2 issues 2–7 are buildable fail-fast stubs so the ADR-0071 CHECK↔registry parity test stays green the moment migration 0020 lands. A stdlib-only gate script measures cumulative touched core lines against the `pre-M2` tag.

**Tech Stack:** Python 3.13, libvirt-python (injected, never opened in unit tests), psycopg, boto3, pytest, GitHub Actions.

**Spec / ADRs:** `docs/specs/m2-remote-libvirt.md` §Decomposition issue 1, ADR-0076, ADR-0077.

---

## File structure

| File | Responsibility |
|------|----------------|
| `src/kdive/domain/models.py` (modify) | `ResourceKind.REMOTE_LIBVIRT = "remote-libvirt"` (allowlisted core touch-point) |
| `src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql` (create) | CHECK widen (the one M2 migration; allowlisted) |
| `src/kdive/store/objectstore.py` (modify) | `presign_get` (the one additive core primitive; allowlisted) |
| `src/kdive/providers/remote_libvirt/__init__.py` (create) | Package docstring citing ADR-0076/0077 |
| `src/kdive/providers/remote_libvirt/config.py` (create) | Operator env config (`KDIVE_REMOTE_LIBVIRT_*`), opt-in detection, fail-fast validation |
| `src/kdive/providers/remote_libvirt/transport.py` (create) | URI validation (`no_verify` forbidden), pkipath materialize/cleanup, connection context manager |
| `src/kdive/providers/remote_libvirt/discovery.py` (create) | `RemoteLibvirtDiscovery` over the injected TLS connection → `ResourceRecord` capabilities |
| `src/kdive/providers/remote_libvirt/planes.py` (create) | Buildable fail-fast stub ports for issues 2–7 planes |
| `src/kdive/providers/composition.py` (modify) | `build_remote_runtime`, opt-in gate, discovery registrar (in `providers/`, outside the gated core) |
| `scripts/m2_portability_gate.py` (create) | Cumulative touched-lines gate vs `pre-M2` |
| `.github/workflows/ci.yml` (modify) | `m2-portability` job (fetch-depth 0) |
| `justfile` (modify) | `m2-gate` recipe; appended to `ci` |
| Tests | `tests/providers/remote_libvirt/` (new), `tests/store/test_objectstore.py`, `tests/providers/test_composition.py`, `tests/db/test_resource_kind_parity.py`, `tests/db/test_migrate.py`, `tests/scripts/test_m2_portability_gate.py` |

**Commit-ordering invariant:** migration 0020 and the composition registration land in the *same commit* (Task 7) so the parity test never sees a CHECK-admitted kind without a buildable runtime. The enum value alone (Task 1) is safe earlier: nothing registers or admits it yet.

**Decisions pinned for this plan** (from spec/ADRs; do not relitigate):

- Env names: `KDIVE_REMOTE_LIBVIRT_URI`, `KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF`, `KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF`, `KDIVE_REMOTE_LIBVIRT_CA_CERT_REF`, `KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP` (default 1). Opt-in = explicit flag, else URI env present.
- pkipath file names are libvirt's fixed lookup names: `clientcert.pem`, `clientkey.pem`, `cacert.pem`.
- Stub planes raise `CategorizedError` with `ErrorCategory.MISSING_DEPENDENCY` (the ports' documented category for unavailable provider seams). No new category (spec §Error taxonomy).
- Cost class reuses the seeded `local` coefficient (a `remote` seed row would be new core DDL beyond migration 0020 — the gate firing). Same precedent as fault-inject.
- `supported_capture_methods = frozenset({CaptureMethod.KDUMP, CaptureMethod.GDBSTUB})` — ADR-0078's two-phase kdump is the vmcore path; gdbstub is the debug tier. Later issues may widen.
- pkipath cleanup failure on an otherwise-successful op: log at error level, do not raise over an in-flight exception (raising would replace the op's typed error; the exposure is bounded by worker-local storage per ADR-0077).
- ADR statuses stay `Proposed` (ADR-0071 precedent: implementation does not flip the field).
- No PCIe enumeration and no `list_owned` reaping for remote in this issue — both need domains/host introspection that issue 2 creates.

---

### Task 1: `ResourceKind.REMOTE_LIBVIRT`

**Files:**
- Modify: `src/kdive/domain/models.py:53-62`
- Test: `tests/db/test_resource_kind_parity.py` (no change yet — parity still `{local-libvirt, fault-inject}` until Task 7)

- [ ] **Step 1: Add the enum value with docstring**

```python
class ResourceKind(StrEnum):
    """The provider resource kinds.

    Production defaults to ``LOCAL_LIBVIRT``. ``FAULT_INJECT`` is a concrete opt-in mock
    provider behind the same ``ProviderResolver`` seam and is absent from default
    production composition. ``REMOTE_LIBVIRT`` (ADR-0076) is the M2 remote provider,
    opt-in by operator config (a ``qemu+tls://`` host URI + TLS cert refs).
    """

    LOCAL_LIBVIRT = "local-libvirt"
    FAULT_INJECT = "fault-inject"
    REMOTE_LIBVIRT = "remote-libvirt"
```

- [ ] **Step 2: Run the adjacent suites to confirm nothing keys exhaustively off the enum**

Run: `uv run python -m pytest tests/db tests/providers tests/domain -q` (db suite needs Docker; it skips without)
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/kdive/domain/models.py
git commit -m "feat: add ResourceKind.REMOTE_LIBVIRT enum value"
```

### Task 2: `presign_get` object-store primitive

**Files:**
- Modify: `src/kdive/store/objectstore.py` (after `presign_put`)
- Test: `tests/store/test_objectstore.py`

- [ ] **Step 1: Write the failing tests** (mirror the file's existing fake-client style; read the file's fixtures first and reuse them)

```python
def test_presign_get_mints_time_boxed_url_for_one_key() -> None:
    client = _FakePresignClient()  # records generate_presigned_url kwargs, returns a URL
    store = ObjectStore(client, "bucket")
    url = store.presign_get("t/vmcore/abc/core", expires_in=600)
    assert url == client.minted_url
    assert client.calls == [
        (
            "get_object",
            {"Bucket": "bucket", "Key": "t/vmcore/abc/core"},
            600,
            "GET",
        )
    ]


def test_presign_get_rejects_non_positive_expiry() -> None:
    store = ObjectStore(_FakePresignClient(), "bucket")
    with pytest.raises(CategorizedError) as exc:
        store.presign_get("k", expires_in=0)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_presign_get_maps_client_error_to_infrastructure_failure() -> None:
    client = _FakePresignClient(raises=ClientError({"Error": {"Code": "boom"}}, "presign"))
    store = ObjectStore(client, "bucket")
    with pytest.raises(CategorizedError) as exc:
        store.presign_get("k", expires_in=60)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/store/test_objectstore.py -q` — Expected: FAIL (`presign_get` missing)

- [ ] **Step 3: Implement**

```python
    def presign_get(self, key: str, *, expires_in: int) -> str:
        """Mint a time-boxed presigned GET URL for one object (ADR-0076, ADR-0078).

        The URL is a bearer capability scoped to ``key`` alone, expiring after
        ``expires_in`` seconds. Callers that hand it across a trust boundary must
        register it in the redaction registry before it leaves the worker
        (ADR-0078 §2 — the in-target seam, M2 issue 3).

        Raises:
            CategorizedError: ``expires_in`` is not positive
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`), or presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        if expires_in <= 0:
            raise CategorizedError(
                f"presign_get for {key!r} needs a positive expiry, got {expires_in}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"key": key},
            )
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_get", key, err) from err
```

- [ ] **Step 4: Run** — `uv run python -m pytest tests/store/test_objectstore.py -q` — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: add presign_get object-store primitive"`

### Task 3: remote-libvirt operator config

**Files:**
- Create: `src/kdive/providers/remote_libvirt/__init__.py`, `src/kdive/providers/remote_libvirt/config.py`
- Test: `tests/providers/remote_libvirt/__init__.py`, `tests/providers/remote_libvirt/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Operator config for the remote-libvirt provider (ADR-0076, ADR-0077)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import (
    is_remote_libvirt_configured,
    remote_config_from_env,
)

_ENV = {
    "KDIVE_REMOTE_LIBVIRT_URI": "qemu+tls://host.example/system",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF": "remote/clientcert.pem",
    "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF": "remote/clientkey.pem",  # pragma: allowlist secret - a ref, not a value
    "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF": "remote/cacert.pem",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    merged: dict[str, str | None] = {**_ENV, **overrides}
    for name, value in merged.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_full_env_builds_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    config = remote_config_from_env()
    assert config.uri == "qemu+tls://host.example/system"
    assert config.cert_refs.client_cert_ref == "remote/clientcert.pem"
    assert config.cert_refs.client_key_ref == "remote/clientkey.pem"  # pragma: allowlist secret
    assert config.cert_refs.ca_cert_ref == "remote/cacert.pem"
    assert config.concurrent_allocation_cap == 1  # default


def test_configured_detection_tracks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_URI=None)
    assert not is_remote_libvirt_configured()
    _set_env(monkeypatch)
    assert is_remote_libvirt_configured()


@pytest.mark.parametrize(
    "missing",
    [
        "KDIVE_REMOTE_LIBVIRT_URI",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF",
        "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF",
        "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
    ],
)
def test_missing_env_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_env(monkeypatch, **{missing: None})
    with pytest.raises(CategorizedError) as exc:
        remote_config_from_env()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert missing in str(exc.value)


def test_non_integer_cap_is_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP="two")
    with pytest.raises(CategorizedError) as exc:
        remote_config_from_env()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_explicit_cap_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP="4")
    assert remote_config_from_env().concurrent_allocation_cap == 4
```

URI-shape rejections (scheme, `no_verify`, operator-set `pkipath`) are tested in Task 4 against `validate_remote_uri`, which `remote_config_from_env` calls — add one integration check here:

```python
def test_uri_with_no_verify_is_rejected_at_config_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(
        monkeypatch,
        KDIVE_REMOTE_LIBVIRT_URI="qemu+tls://host.example/system?no_verify=1",
    )
    with pytest.raises(CategorizedError) as exc:
        remote_config_from_env()
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/providers/remote_libvirt/ -q` — Expected: FAIL (module missing)

- [ ] **Step 3: Implement `config.py`** (and an `__init__.py` with a docstring citing ADR-0076/0077)

```python
"""Operator configuration for the remote-libvirt provider (ADR-0076, ADR-0077).

The provider is opt-in: composition registers it only when the operator supplies a
``qemu+tls://`` host URI. The TLS client cert, key, and CA are secrets-by-reference
(``SecretBackend`` refs), never material. Reading the config is deferred to
discovery/connection time so the runtime stays buildable without it (ADR-0076).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.transport import validate_remote_uri

_URI_ENV = "KDIVE_REMOTE_LIBVIRT_URI"
_CLIENT_CERT_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF"
_CLIENT_KEY_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF"  # pragma: allowlist secret - env var name
_CA_CERT_REF_ENV = "KDIVE_REMOTE_LIBVIRT_CA_CERT_REF"
_CAP_ENV = "KDIVE_REMOTE_LIBVIRT_ALLOCATION_CAP"
_DEFAULT_CAP = 1


@dataclass(frozen=True, slots=True)
class TlsCertRefs:
    """Secret references (not material) for the mutual-TLS client identity + CA."""

    client_cert_ref: str
    client_key_ref: str
    ca_cert_ref: str


@dataclass(frozen=True, slots=True)
class RemoteLibvirtConfig:
    """The operator-supplied remote host: validated URI, cert refs, allocation cap."""

    uri: str
    cert_refs: TlsCertRefs
    concurrent_allocation_cap: int


def is_remote_libvirt_configured() -> bool:
    """True when the operator supplied a remote host URI (the composition opt-in gate)."""
    return bool(os.environ.get(_URI_ENV))


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CategorizedError(
            f"{name} is not set; the remote-libvirt provider needs it",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    return value


def remote_config_from_env() -> RemoteLibvirtConfig:
    """Read and validate the ``KDIVE_REMOTE_LIBVIRT_*`` operator config.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a missing/blank variable, a
            non-integer allocation cap, or a URI that is not mutual-TLS-safe
            (wrong scheme, ``no_verify``, or an operator-set ``pkipath``).
    """
    uri = _required_env(_URI_ENV)
    validate_remote_uri(uri)
    refs = TlsCertRefs(
        client_cert_ref=_required_env(_CLIENT_CERT_REF_ENV),
        client_key_ref=_required_env(_CLIENT_KEY_REF_ENV),
        ca_cert_ref=_required_env(_CA_CERT_REF_ENV),
    )
    raw_cap = os.environ.get(_CAP_ENV)
    if raw_cap is None:
        cap = _DEFAULT_CAP
    else:
        try:
            cap = int(raw_cap)
        except ValueError:
            raise CategorizedError(
                f"{_CAP_ENV}={raw_cap!r} is not an integer",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None
    return RemoteLibvirtConfig(uri=uri, cert_refs=refs, concurrent_allocation_cap=cap)
```

Note: `config.py` imports `validate_remote_uri` from `transport.py`, so Task 4's `transport.py` must exist before this module imports cleanly. Implement Task 4 Step 3's `validate_remote_uri` together with this step (the tests for it run in Task 4); keep the commits separate only if both stay green — otherwise fold Tasks 3+4 into one commit.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/providers/remote_libvirt/test_config.py -q` — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: add remote-libvirt operator config module"`

### Task 4: transport — URI validation, pkipath lifecycle, connection context

**Files:**
- Create: `src/kdive/providers/remote_libvirt/transport.py`
- Test: `tests/providers/remote_libvirt/test_transport.py`

- [ ] **Step 1: Write the failing tests**

```python
"""qemu+tls transport: URI validation, pkipath lifecycle, connection context (ADR-0077)."""

from __future__ import annotations

import stat
from pathlib import Path
from urllib.parse import unquote

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.transport import (
    compose_pkipath_uri,
    materialized_pkipath,
    remote_connection,
    validate_remote_uri,
)

_REFS = TlsCertRefs(
    client_cert_ref="remote/clientcert.pem",
    client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
    ca_cert_ref="remote/cacert.pem",
)


class _RecordingBackend:
    """SecretBackend test double returning a distinct PEM body per ref."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> str:
        self.resolved.append(ref)
        return f"PEM::{ref}"


def _config(uri: str = "qemu+tls://host.example/system") -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(uri=uri, cert_refs=_REFS, concurrent_allocation_cap=1)


@pytest.mark.parametrize(
    "uri",
    [
        "qemu+ssh://host.example/system",
        "qemu:///system",
        "qemu+tcp://host.example/system",
        "qemu+tls://host.example/system?no_verify=1",
        "qemu+tls://host.example/system?no_verify=0",
        "qemu+tls://host.example/system?pkipath=/operator/pki",
    ],
)
def test_validate_rejects_unsafe_uris(uri: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_remote_uri(uri)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_accepts_plain_tls_uri() -> None:
    validate_remote_uri("qemu+tls://host.example/system")


def test_compose_appends_pkipath_preserving_query() -> None:
    uri = compose_pkipath_uri(
        "qemu+tls://host.example/system?keepalive_interval=5", Path("/tmp/pki")
    )
    assert uri == "qemu+tls://host.example/system?keepalive_interval=5&pkipath=%2Ftmp%2Fpki"


def test_pkipath_materializes_private_files_and_cleans_up(tmp_path: Path) -> None:
    backend = _RecordingBackend()
    with materialized_pkipath(backend, _REFS, base_dir=tmp_path) as pkipath:
        assert stat.S_IMODE(pkipath.stat().st_mode) == 0o700
        for name, ref in [
            ("clientcert.pem", "remote/clientcert.pem"),
            ("clientkey.pem", "remote/clientkey.pem"),
            ("cacert.pem", "remote/cacert.pem"),
        ]:
            file = pkipath / name
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            assert file.read_text() == f"PEM::{ref}"
    assert backend.resolved == [
        "remote/clientcert.pem",
        "remote/clientkey.pem",
        "remote/cacert.pem",
    ]
    assert list(tmp_path.iterdir()) == []  # deleted on the success path


def test_pkipath_cleans_up_when_body_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with materialized_pkipath(_RecordingBackend(), _REFS, base_dir=tmp_path):
            raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def getInfo(self) -> list[object]:  # noqa: N802 - libvirt binding name
        return ["x86_64", 16384, 8, 2400, 1, 1, 8, 1]

    def getCapabilities(self) -> str:  # noqa: N802 - libvirt binding name
        return "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"

    def close(self) -> None:
        self.closed = True


def test_remote_connection_opens_with_pkipath_uri_and_cleans_up(tmp_path: Path) -> None:
    opened: list[str] = []
    conn = _FakeConn()

    def open_connection(uri: str) -> _FakeConn:
        opened.append(uri)
        # The pkipath must exist (with the key) while the TLS handshake runs.
        pki = Path(unquote(uri.rsplit("pkipath=", 1)[1]))
        assert (pki / "clientkey.pem").is_file()
        return conn

    with remote_connection(
        _config(), _RecordingBackend(), open_connection=open_connection, pki_base_dir=tmp_path
    ) as got:
        assert got is conn
    assert conn.closed
    assert list(tmp_path.iterdir()) == []
    assert opened[0].startswith("qemu+tls://host.example/system?pkipath=")


def test_remote_connection_maps_open_failure_to_transport_failure(tmp_path: Path) -> None:
    import libvirt

    def open_connection(uri: str) -> _FakeConn:
        raise libvirt.libvirtError("handshake failed")

    with pytest.raises(CategorizedError) as exc:
        with remote_connection(
            _config(),
            _RecordingBackend(),
            open_connection=open_connection,
            pki_base_dir=tmp_path,
        ):
            pytest.fail("body must not run")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert list(tmp_path.iterdir()) == []  # cleaned up on the failure path too


def test_remote_connection_closes_conn_when_body_raises(tmp_path: Path) -> None:
    conn = _FakeConn()
    with pytest.raises(RuntimeError, match="op failed"):
        with remote_connection(
            _config(),
            _RecordingBackend(),
            open_connection=lambda _uri: conn,
            pki_base_dir=tmp_path,
        ):
            raise RuntimeError("op failed")
    assert conn.closed
    assert list(tmp_path.iterdir()) == []
```

(Note: `libvirt.libvirtError("...")` is constructible with a message in libvirt-python; if the binding refuses, raise it via `libvirt.libvirtError` subclass instantiation in the test — check at implementation time.)

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (module missing)

- [ ] **Step 3: Implement `transport.py`**

```python
"""qemu+tls:// connection lifecycle for the remote-libvirt provider (ADR-0077).

Mutual TLS, fail-closed: the worker presents a client cert and verifies the libvirtd
server cert against the configured CA + hostname; ``no_verify`` is forbidden. Because
``SecretBackend.resolve`` returns strings while libvirt's TLS client reads on-disk
files, each op materializes the resolved cert/key/CA into a private per-op pkipath
(dir ``0700``, files ``0600``), points the URI at it via ``?pkipath=``, and deletes
the directory on every exit path. The on-disk lifetime, not text masking, is the
control for the private key (it is consumed by the TLS layer and never echoed).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.secrets.secrets import SecretBackend

if TYPE_CHECKING:
    from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs

_REQUIRED_SCHEME = "qemu+tls"
# libvirt resolves exactly these names inside a pkipath.
_CLIENT_CERT_NAME = "clientcert.pem"
_CLIENT_KEY_NAME = "clientkey.pem"  # pragma: allowlist secret - libvirt file name
_CA_CERT_NAME = "cacert.pem"
_log = logging.getLogger(__name__)


class _LibvirtConn(Protocol):
    """The slice of a libvirt connection the remote provider uses (duck-typed seam)."""

    def getInfo(self) -> list[Any]: ...  # noqa: N802 - libvirt binding name
    def getCapabilities(self) -> str: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenConnection = Callable[[str], _LibvirtConn]


def validate_remote_uri(uri: str) -> None:
    """Reject any URI that would weaken mutual TLS (fail-closed, ADR-0077).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-``qemu+tls`` scheme, a
            ``no_verify`` parameter (server-cert verification must stay on), or an
            operator-set ``pkipath`` (each op composes its own private pkipath).
    """
    parsed = urlsplit(uri)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise CategorizedError(
            f"remote-libvirt URI {uri!r} must use the qemu+tls:// scheme",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "no_verify" in query:
        raise CategorizedError(
            "no_verify is forbidden on the remote-libvirt URI: server-cert "
            "verification is mandatory (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "pkipath" in query:
        raise CategorizedError(
            "pkipath must not be set on the remote-libvirt URI: each op "
            "materializes its own private pkipath (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def compose_pkipath_uri(uri: str, pkipath: Path) -> str:
    """Append ``pkipath=<dir>`` to the URI query, preserving existing parameters."""
    parsed = urlsplit(uri)
    pki_param = f"pkipath={quote(str(pkipath), safe='')}"
    query = f"{parsed.query}&{pki_param}" if parsed.query else pki_param
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)


@contextmanager
def materialized_pkipath(
    secret_backend: SecretBackend, refs: TlsCertRefs, *, base_dir: Path | None = None
) -> Iterator[Path]:
    """Resolve the cert/key/CA refs into a private per-op pkipath; delete on every exit.

    The directory is ``0700`` (``mkdtemp``), files ``0600``. Resolution goes through
    ``SecretBackend`` so each value registers into the redaction registry before use
    (defense-in-depth, ADR-0027); the primary control is the bounded on-disk lifetime.
    A cleanup failure is logged at error level rather than raised, so it never
    replaces the op's typed in-flight error; the residue is bounded by worker-local
    storage (ADR-0077).
    """
    client_cert = secret_backend.resolve(refs.client_cert_ref)
    client_key = secret_backend.resolve(refs.client_key_ref)
    ca_cert = secret_backend.resolve(refs.ca_cert_ref)
    pkipath = Path(tempfile.mkdtemp(prefix="kdive-remote-pki-", dir=base_dir))
    try:
        _write_private(pkipath / _CLIENT_CERT_NAME, client_cert)
        _write_private(pkipath / _CLIENT_KEY_NAME, client_key)
        _write_private(pkipath / _CA_CERT_NAME, ca_cert)
        yield pkipath
    finally:
        try:
            shutil.rmtree(pkipath)
        except OSError:
            _log.exception(
                "failed to delete pkipath %s; private key material may remain on disk",
                pkipath,
            )


@contextmanager
def remote_connection(
    config: RemoteLibvirtConfig,
    secret_backend: SecretBackend,
    *,
    open_connection: OpenConnection,
    pki_base_dir: Path | None = None,
) -> Iterator[_LibvirtConn]:
    """Open a mutual-TLS libvirt connection for one op; close it and the pkipath after.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe URI, or
            ``TRANSPORT_FAILURE`` when the TLS connect fails.
    """
    validate_remote_uri(config.uri)
    with materialized_pkipath(secret_backend, config.cert_refs, base_dir=pki_base_dir) as pki:
        uri = compose_pkipath_uri(config.uri, pki)
        try:
            conn = open_connection(uri)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                f"qemu+tls connect to {config.uri!r} failed",
                category=ErrorCategory.TRANSPORT_FAILURE,
                details={"uri": config.uri},
            ) from exc
        try:
            yield conn
        finally:
            conn.close()


def open_libvirt(uri: str) -> _LibvirtConn:
    """The production opener (`live_vm`-only path; unit tests inject a fake)."""
    # libvirt ships no stubs; the connection is duck-typed at the seam — scoped ignore.
    return libvirt.open(uri)  # ty: ignore[invalid-return-type]
```

(The exact `ty` ignore code may differ — run `just type` and use the reported code, mirroring `local_libvirt/discovery.py:175`.)

- [ ] **Step 4: Run** — `uv run python -m pytest tests/providers/remote_libvirt/ -q` — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: add remote-libvirt qemu+tls transport with pkipath lifecycle"`

### Task 5: discovery over TLS

**Files:**
- Create: `src/kdive/providers/remote_libvirt/discovery.py`
- Test: `tests/providers/remote_libvirt/test_discovery.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Remote-libvirt discovery over the injected TLS connection (ADR-0076)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery

# Reuse _RecordingBackend and _FakeConn from test_transport via a tiny conftest.py
from tests.providers.remote_libvirt.conftest import _FakeConn, _RecordingBackend  # adjust to fixtures


def test_list_resources_returns_remote_record(tmp_path: Path) -> None:
    refs = TlsCertRefs(
        client_cert_ref="remote/clientcert.pem",
        client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
        ca_cert_ref="remote/cacert.pem",
    )
    config = RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system", cert_refs=refs, concurrent_allocation_cap=2
    )
    conn = _FakeConn()
    discovery = RemoteLibvirtDiscovery(
        config=config,
        secret_backend=_RecordingBackend(),
        open_connection=lambda _uri: conn,
        pki_base_dir=tmp_path,
    )
    records = discovery.list_resources()
    assert len(records) == 1
    record = records[0]
    assert record.kind is ResourceKind.REMOTE_LIBVIRT
    assert record.resource_id == "qemu+tls://host.example/system"
    assert record.status is ResourceStatus.AVAILABLE
    caps = record.capabilities
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps["connect_uri"] == "qemu+tls://host.example/system"
    assert caps["tls_client_cert_ref"] == "remote/clientcert.pem"
    assert caps["tls_client_key_ref"] == "remote/clientkey.pem"  # pragma: allowlist secret
    assert caps["tls_ca_cert_ref"] == "remote/cacert.pem"
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 2
    assert conn.closed  # the discovery op closes its connection
    assert list(tmp_path.iterdir()) == []  # and its pkipath


def test_malformed_capabilities_xml_yields_unknown_arch(tmp_path: Path) -> None:
    class _BadXmlConn(_FakeConn):
        def getCapabilities(self) -> str:  # noqa: N802
            return "<not-xml"

    discovery = RemoteLibvirtDiscovery(
        config=...,  # as above
        secret_backend=_RecordingBackend(),
        open_connection=lambda _uri: _BadXmlConn(),
        pki_base_dir=tmp_path,
    )
    assert discovery.list_resources()[0].capabilities["arch"] == "unknown"


def test_from_env_without_uri_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.security.secrets.secret_registry import SecretRegistry

    with pytest.raises(CategorizedError) as exc:
        RemoteLibvirtDiscovery.from_env(secret_registry=SecretRegistry())
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
```

Move `_FakeConn` / `_RecordingBackend` into `tests/providers/remote_libvirt/conftest.py` as plain importable classes (or fixtures) shared by the transport and discovery tests; update Task 4's test imports accordingly.

- [ ] **Step 2: Run to verify failure** — Expected: FAIL

- [ ] **Step 3: Implement `discovery.py`**

```python
"""Remote-libvirt Discovery plane over qemu+tls (ADR-0076, ADR-0077).

Enumerates the remote host over an injected mutual-TLS connection (unit tests never
touch a real host; the real ``libvirt.open`` adapter is the production opener) and
advertises arch/cpu/memory, the gdbstub transport, the connect URI + TLS secret refs,
and the per-host concurrent-Allocation cap into ``resources.capabilities``.

The XML parse is duplicated from local-libvirt deliberately: no shared
``libvirt_common`` layer (ADR-0076 — local-libvirt is headed for removal).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.discovery import ResourceRecord
from kdive.domain.models import ResourceKind
from kdive.domain.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.state import ResourceStatus
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_env
from kdive.providers.remote_libvirt.transport import (
    OpenConnection,
    open_libvirt,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env


def _parse_arch(caps_xml: str) -> str:
    """Read ``<host><cpu><arch>``; ``unknown`` if absent/malformed (defusedxml — the
    XML crosses the libvirtd trust boundary; an attack document raises, fail loud)."""
    try:
        root: ET.Element = _safe_fromstring(caps_xml)
    except ET.ParseError:
        return "unknown"
    return root.findtext("./host/cpu/arch") or "unknown"


class RemoteLibvirtDiscovery:
    """The realized discovery port for one remote qemu+tls host."""

    def __init__(
        self,
        *,
        config: RemoteLibvirtConfig,
        secret_backend: SecretBackend,
        open_connection: OpenConnection,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._secret_backend = secret_backend
        self._open_connection = open_connection
        self._pki_base_dir = pki_base_dir
        self.host_uri = config.uri

    @classmethod
    def from_env(cls, *, secret_registry: SecretRegistry) -> RemoteLibvirtDiscovery:
        """Build from ``KDIVE_REMOTE_LIBVIRT_*`` (raises ``CONFIGURATION_ERROR`` when unset)."""
        return cls(
            config=remote_config_from_env(),
            secret_backend=secret_backend_from_env(registry=secret_registry),
            open_connection=open_libvirt,
        )

    def list_resources(self) -> list[ResourceRecord]:
        """Return one ``ResourceRecord`` for the remote host (resource id = the URI)."""
        with remote_connection(
            self._config,
            self._secret_backend,
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        ) as conn:
            info = conn.getInfo()
            arch = _parse_arch(conn.getCapabilities())
        refs = self._config.cert_refs
        capabilities: dict[str, Any] = {
            "arch": arch,
            "vcpus": int(info[2]),
            "memory_mb": int(info[1]),
            "transports": ["gdbstub"],
            "connect_uri": self._config.uri,
            "tls_client_cert_ref": refs.client_cert_ref,
            "tls_client_key_ref": refs.client_key_ref,
            "tls_ca_cert_ref": refs.ca_cert_ref,
            CONCURRENT_ALLOCATION_CAP_KEY: self._config.concurrent_allocation_cap,
        }
        return [
            ResourceRecord(
                resource_id=self.host_uri,
                kind=ResourceKind.REMOTE_LIBVIRT,
                capabilities=capabilities,
                status=ResourceStatus.AVAILABLE,
            )
        ]
```

(If `os` ends up unused, drop the import. PCIe enumeration and `list_owned` reaping are deferred to M2 issue 2, which creates the domains they would inspect.)

- [ ] **Step 4: Run** — `uv run python -m pytest tests/providers/remote_libvirt/ -q` — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: add remote-libvirt discovery over qemu+tls"`

### Task 6: stub planes (buildable, fail-fast)

**Files:**
- Create: `src/kdive/providers/remote_libvirt/planes.py`
- Test: `tests/providers/remote_libvirt/test_planes.py`

- [ ] **Step 1: Write the failing test**

```python
"""Remote-libvirt stub planes fail fast with a typed error (ADR-0076)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt import planes


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: planes.UnimplementedProvisioner().provision(uuid4(), None),
        lambda: planes.UnimplementedProvisioner().teardown(uuid4()),
        lambda: planes.UnimplementedBuilder().build(uuid4(), None),
        lambda: planes.UnimplementedInstaller().install(None),
        lambda: planes.UnimplementedInstaller().boot(uuid4()),
        lambda: planes.UnimplementedConnector().open_transport(None, "gdbstub"),
        lambda: planes.UnimplementedConnector().close_transport(None),
        lambda: planes.UnimplementedController().power("dom", None),
        lambda: planes.UnimplementedController().force_crash("dom"),
        lambda: planes.UnimplementedRetriever().capture(uuid4(), None),
        lambda: planes.UnimplementedRetriever().run_crash_postmortem(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="b", commands=[]
        ),
        lambda: planes.UnimplementedIntrospector().from_vmcore(
            vmcore_ref="v", debuginfo_ref="d", expected_build_id="b"
        ),
        lambda: planes.UnimplementedIntrospector().introspect_live(
            transport_handle="t", helper="h"
        ),
    ],
)
def test_every_stub_plane_raises_missing_dependency(invoke) -> None:
    with pytest.raises(CategorizedError) as exc:
        invoke()
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert "remote-libvirt" in str(exc.value)
```

Adjust the lambda arguments to the real port signatures in `src/kdive/providers/ports/` (read `lifecycle.py`, `build.py`, `retrieve.py` first — e.g. `Provisioner.teardown` may differ; mirror exactly, passing typed `None`/sentinels via `cast` where ty needs it).

- [ ] **Step 2: Run to verify failure** — Expected: FAIL

- [ ] **Step 3: Implement `planes.py`** — one class per port group, every method delegating to a single helper:

```python
"""Unimplemented remote-libvirt planes — buildable, fail-fast stubs (ADR-0076).

M2 issue 1 lands the package, kind, transport, and discovery; provisioning, the
artifact seam, build, install, connect/debug, and control/retrieve land in M2
issues 2–7. Until then each plane raises a typed ``MISSING_DEPENDENCY`` (the ports'
documented category for an unavailable provider seam) so the runtime is buildable
— the ADR-0071 CHECK↔registry parity invariant — without pretending the plane works.
"""

from __future__ import annotations

from typing import NoReturn

from kdive.domain.errors import CategorizedError, ErrorCategory


def _unimplemented(plane: str) -> NoReturn:
    raise CategorizedError(
        f"remote-libvirt {plane} is not implemented yet (a later M2 change supplies it)",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"plane": plane},
    )


class UnimplementedProvisioner:
    def provision(self, system_id, profile) -> str:
        _unimplemented("provisioning")

    def teardown(self, system_id) -> None:
        _unimplemented("provisioning")
    # ...mirror every Provisioner protocol method
```

…and likewise `UnimplementedBuilder` (Builder), `UnimplementedInstaller` (Installer + Booter), `UnimplementedConnector` (Connector), `UnimplementedController` (Controller), `UnimplementedRetriever` (Retriever + CrashPostmortem), `UnimplementedIntrospector` (VmcoreIntrospector + LiveIntrospector). Match each protocol's full method set and signatures (with type annotations) so `ProviderRuntime(...)` type-checks structurally under `just type`.

- [ ] **Step 4: Run** — `uv run python -m pytest tests/providers/remote_libvirt/ -q` — Expected: PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: add remote-libvirt fail-fast stub planes"`

### Task 7: composition registration + migration 0020 + parity (one commit)

**Files:**
- Create: `src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql`
- Modify: `src/kdive/providers/composition.py`
- Test: `tests/db/test_migrate.py`, `tests/db/test_resource_kind_parity.py`, `tests/providers/test_composition.py`

- [ ] **Step 1: Write the failing tests**

`tests/db/test_migrate.py`: append `"0020"` to the expected list in `test_rerun_is_a_noop`.

`tests/db/test_resource_kind_parity.py`:

```python
def test_check_admits_all_three_kinds(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    # 0018 widened for fault-inject; 0020 widens for remote-libvirt (ADR-0076).
    assert _check_allowed_kinds(pg_conn) == {"local-libvirt", "fault-inject", "remote-libvirt"}
```

(rename/replace the old two-kind test), and in the two buildable-universe tests change the resolver construction to:

```python
    resolver = build_provider_resolver(enable_fault_inject=True, enable_remote_libvirt=True)
```

`test_default_production_registry_registers_only_local_libvirt` stays as-is (and now also proves no remote runtime by default). Add an env-driven case to `tests/providers/test_composition.py`:

```python
def test_remote_libvirt_registers_via_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_URI", "qemu+tls://host.example/system")
    resolver = composition.build_provider_resolver()
    assert ResourceKind.REMOTE_LIBVIRT in resolver.registered_kinds()


def test_remote_libvirt_explicit_flag_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_URI", "qemu+tls://host.example/system")
    resolver = composition.build_provider_resolver(enable_remote_libvirt=False)
    assert ResourceKind.REMOTE_LIBVIRT not in resolver.registered_kinds()


def test_remote_runtime_buildable_without_operator_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_URI", raising=False)
    # Buildability gates only construction (ADR-0076); config gates discovery/connection.
    runtime = composition.build_remote_runtime(secret_registry=SecretRegistry())
    assert runtime.discovery_registrar is not None
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/providers/test_composition.py -q` — Expected: FAIL (`build_remote_runtime` missing); db tests fail on the CHECK set.

- [ ] **Step 3: Create the migration**

```sql
-- 0020_resources_kind_remote_libvirt.sql — M2 remote-libvirt resource kind (ADR-0076).
-- Additive to 0001 (forward-only, ADR-0015). Widens the resources.kind CHECK to admit the
-- `remote-libvirt` provider kind alongside `local-libvirt` and `fault-inject`; mirrors
-- ResourceKind in domain/models.py and lands with the runtime that registers it (M2
-- issue 1), so the CHECK<->registry parity test never sees a CHECK-allowed kind without
-- a buildable runtime. Drop-and-recreate keeps the constraint name stable.
ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
ALTER TABLE resources ADD CONSTRAINT resources_kind_check
    CHECK (kind IN ('local-libvirt', 'fault-inject', 'remote-libvirt'));
```

- [ ] **Step 4: Wire composition** — in `src/kdive/providers/composition.py` add:

```python
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery
from kdive.providers.remote_libvirt.planes import (
    UnimplementedBuilder,
    UnimplementedConnector,
    UnimplementedController,
    UnimplementedInstaller,
    UnimplementedIntrospector,
    UnimplementedProvisioner,
    UnimplementedRetriever,
)

_REMOTE_POOL = "remote-libvirt"
# Reuses the seeded `local` coefficient: a `remote` seed row would be core DDL beyond
# migration 0020 (the portability gate firing). Same precedent as fault-inject.
_REMOTE_COST_CLASS = "local"


def _remote_component_sources() -> ComponentSourceCapabilities:
    # Empty until the build/install issues define what the remote provider accepts.
    return ComponentSourceCapabilities(
        provider=ResourceKind.REMOTE_LIBVIRT.value, accepted_component_sources={}
    )


def build_remote_runtime(*, secret_registry: SecretRegistry) -> ProviderRuntime:
    """Build the remote-libvirt ports; buildable without operator config (ADR-0076).

    Construction wires the fail-fast stub planes and the discovery registrar; the
    ``KDIVE_REMOTE_LIBVIRT_*`` config gates discovery/connection and is read only when
    the registrar runs.
    """
    installer = UnimplementedInstaller()
    retriever = UnimplementedRetriever()
    introspector = UnimplementedIntrospector()

    async def register_remote_host(pool: AsyncConnectionPool) -> None:
        discovery = RemoteLibvirtDiscovery.from_env(secret_registry=secret_registry)
        await ensure_discovered_resource_registered(
            pool,
            discovery,
            kind=ResourceKind.REMOTE_LIBVIRT,
            resource_id=discovery.host_uri,
            pool_name=_REMOTE_POOL,
            cost_class=_REMOTE_COST_CLASS,
        )

    return ProviderRuntime(
        provisioner=UnimplementedProvisioner(),
        builder=UnimplementedBuilder(),
        installer=installer,
        booter=installer,
        connector=UnimplementedConnector(),
        controller=UnimplementedController(),
        retriever=retriever,
        crash_postmortem=retriever,
        vmcore_introspector=introspector,
        live_introspector=introspector,
        supported_capture_methods=frozenset({CaptureMethod.KDUMP, CaptureMethod.GDBSTUB}),
        discovery_registrar=register_remote_host,
        component_sources=_remote_component_sources(),
    )


def _remote_libvirt_enabled(enable_remote_libvirt: bool | None) -> bool:
    """Resolve the opt-in gate: an explicit flag wins, else operator config presence."""
    if enable_remote_libvirt is not None:
        return enable_remote_libvirt
    return is_remote_libvirt_configured()
```

Extend `ProviderComposition.build_provider_resolver` and the module-level `build_provider_resolver` with `enable_remote_libvirt: bool | None = None`:

```python
    def build_provider_resolver(
        self,
        *,
        enable_fault_inject: bool | None = None,
        enable_remote_libvirt: bool | None = None,
    ) -> ProviderResolver:
        """Assemble the per-deployment ``ResourceKind -> ProviderRuntime`` registry."""
        runtimes = {
            ResourceKind.LOCAL_LIBVIRT: build_local_runtime(secret_registry=self._secret_registry)
        }
        if _fault_inject_enabled(enable_fault_inject):
            runtimes[ResourceKind.FAULT_INJECT] = build_faultinject_runtime(
                inventory=self._faultinject_inventory
            )
        if _remote_libvirt_enabled(enable_remote_libvirt):
            runtimes[ResourceKind.REMOTE_LIBVIRT] = build_remote_runtime(
                secret_registry=self._secret_registry
            )
        return ProviderResolver(runtimes)
```

…pass the parameter through the module-level function, extend its docstring, and add `build_remote_runtime` to `__all__`.

- [ ] **Step 5: Run** — `uv run python -m pytest tests/providers tests/db -q` — Expected: PASS (db suite needs Docker)
- [ ] **Step 6: Commit (single commit — the parity invariant)**

```bash
git add src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql \
        src/kdive/providers/composition.py tests/db tests/providers
git commit -m "feat: register remote-libvirt runtime + migration 0020 CHECK widen"
```

### Task 8: portability gate script

**Files:**
- Create: `scripts/m2_portability_gate.py`
- Test: `tests/scripts/test_m2_portability_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
"""M2 portability gate: cumulative core-touch measurement vs the pre-M2 tag."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.m2_portability_gate import (
    ALLOWED_FILES,
    CORE_PREFIXES,
    parse_numstat,
    violations,
)


def test_parse_numstat_aggregates_per_file_across_commits() -> None:
    out = (
        "3\t1\tsrc/kdive/store/objectstore.py\n"
        "\n"
        "2\t2\tsrc/kdive/store/objectstore.py\n"
        "5\t0\tsrc/kdive/providers/remote_libvirt/config.py\n"
    )
    touched = parse_numstat(out)
    # Cumulative (per-commit sum), not net — a later revert cannot zero it out.
    assert touched["src/kdive/store/objectstore.py"] == 8
    # Non-core paths are not the gate's subject.
    assert "src/kdive/providers/remote_libvirt/config.py" not in touched


def test_parse_numstat_counts_binary_files_as_touched() -> None:
    touched = parse_numstat("-\t-\tsrc/kdive/db/schema/blob.bin\n")
    assert touched["src/kdive/db/schema/blob.bin"] == 1


def test_violations_excludes_allowlisted_files() -> None:
    touched = {
        "src/kdive/store/objectstore.py": 12,
        "src/kdive/services/resources/discovery.py": 4,
    }
    bad = violations(touched)
    assert bad == {"src/kdive/services/resources/discovery.py": 4}


def test_allowlist_is_exactly_the_named_touch_points() -> None:
    assert ALLOWED_FILES == frozenset(
        {
            "src/kdive/domain/models.py",
            "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
            "src/kdive/store/objectstore.py",
        }
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin",
        },
    )


def _write_and_commit(repo: Path, rel: str, content: str, message: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def gate_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_and_commit(repo, "src/kdive/services/svc.py", "x = 1\n", "base")
    _git(repo, "tag", "pre-M2")
    return repo


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[2] / "scripts" / "m2_portability_gate.py"
    return subprocess.run(
        ["python3", str(script)], cwd=repo, capture_output=True, text=True
    )


def test_gate_fails_on_non_allowlisted_core_touch(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 2\n", "leak")
    result = _run_gate(gate_repo)
    assert result.returncode == 1
    assert "src/kdive/services/svc.py" in result.stdout


def test_gate_passes_on_allowlisted_and_provider_touches(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/store/objectstore.py", "def presign_get(): ...\n", "ok")
    _write_and_commit(gate_repo, "src/kdive/providers/remote_libvirt/x.py", "y = 1\n", "provider")
    result = _run_gate(gate_repo)
    assert result.returncode == 0


def test_gate_counts_reverted_changes(gate_repo: Path) -> None:
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 2\n", "leak")
    _write_and_commit(gate_repo, "src/kdive/services/svc.py", "x = 1\n", "revert")
    result = _run_gate(gate_repo)
    assert result.returncode == 1  # cumulative, not net


def test_gate_errors_usefully_without_the_tag(tmp_path: Path) -> None:
    repo = tmp_path / "untagged"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _write_and_commit(repo, "README.md", "hi\n", "base")
    result = _run_gate(repo)
    assert result.returncode == 2
    assert "pre-M2" in result.stderr
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/scripts/test_m2_portability_gate.py -q` — Expected: FAIL

- [ ] **Step 3: Implement `scripts/m2_portability_gate.py`** (stdlib only)

```python
"""Per-PR M2 portability gate (ADR-0076).

Measures the cumulative touched lines (per-commit added+removed — not a net a later
revert can zero out) of every commit since the ``pre-M2`` tag over the
provider-agnostic core (domain/db/jobs/reconciler/services/store/security and the
whole ``mcp`` package including ``mcp/tools/*``), and fails when any file outside the
named allowlist is touched. The allowlist is the ADR-0076 set: the ``ResourceKind``
enum value, the one M2 migration, and the additive ``presign_get`` primitive.
Extending it is a deliberate, reviewed decision — edit this file in the same PR.

Exit codes: 0 gate passes; 1 violations found; 2 the baseline tag is unavailable.
"""

from __future__ import annotations

import subprocess
import sys

BASELINE_TAG = "pre-M2"

CORE_PREFIXES = (
    "src/kdive/domain/",
    "src/kdive/db/",
    "src/kdive/jobs/",
    "src/kdive/reconciler/",
    "src/kdive/services/",
    "src/kdive/store/",
    "src/kdive/security/",
    "src/kdive/mcp/",
)

ALLOWED_FILES = frozenset(
    {
        # ResourceKind.REMOTE_LIBVIRT (ADR-0076 named touch-point).
        "src/kdive/domain/models.py",
        # The one M2 migration: the resources.kind CHECK widen.
        "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
        # The additive presign_get primitive (ADR-0076, ADR-0078).
        "src/kdive/store/objectstore.py",
    }
)


def parse_numstat(out: str) -> dict[str, int]:
    """Aggregate per-file touched lines (added+removed) from ``git log --numstat`` output.

    Binary files render as ``-\t-\tpath`` and count as one touched line. Only files
    under the core prefixes are the gate's subject.
    """
    touched: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path.startswith(CORE_PREFIXES):
            continue
        lines = 1 if added == "-" else int(added) + int(removed)
        touched[path] = touched.get(path, 0) + max(lines, 1)
    return touched


def violations(touched: dict[str, int]) -> dict[str, int]:
    """The non-allowlisted core files with any cumulative touch."""
    return {path: count for path, count in touched.items() if path not in ALLOWED_FILES}


def main() -> int:
    tag_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"{BASELINE_TAG}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if tag_check.returncode != 0:
        print(
            f"error: baseline tag {BASELINE_TAG!r} is unavailable; fetch tags/history "
            "(CI: actions/checkout with fetch-depth: 0)",
            file=sys.stderr,
        )
        return 2
    log = subprocess.run(
        [
            "git",
            "log",
            "--numstat",
            "--no-merges",
            "--no-renames",
            "--format=",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    touched = parse_numstat(log.stdout)
    allowed = {p: n for p, n in touched.items() if p in ALLOWED_FILES}
    print(f"M2 portability measurement since {BASELINE_TAG} (cumulative touched lines):")
    for path, count in sorted(allowed.items()):
        print(f"  allowlisted  {count:>6}  {path}")
    bad = violations(touched)
    if bad:
        print("\ngate FAILED — provider-specific changes reached the core surface:")
        for path, count in sorted(bad.items()):
            print(f"  VIOLATION    {count:>6}  {path}")
        print(
            "\nRefactor the provider logic out of core (the M2 co-equal goal, "
            "docs/specs/m2-remote-libvirt.md), or — for a deliberate provider-agnostic "
            "core change — extend ALLOWED_FILES in this script in the same PR."
        )
        return 1
    print("gate passed: no core surface touched outside the ADR-0076 allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests, then the gate against this very branch**

Run: `uv run python -m pytest tests/scripts/test_m2_portability_gate.py -q` — Expected: PASS
Run: `uv run python scripts/m2_portability_gate.py` — Expected: exit 0, allowlisted lines reported for `models.py`, `0020_…sql`, `objectstore.py`

- [ ] **Step 5: Commit** — `git commit -m "feat: add M2 portability gate script"`

### Task 9: CI job + justfile recipe

**Files:**
- Modify: `.github/workflows/ci.yml`, `justfile`

- [ ] **Step 1: Add the `m2-gate` recipe to the justfile** (and append it to the `ci` recipe chain):

```make
# M2 portability gate: cumulative core-touch measurement vs the pre-M2 tag (ADR-0076).
m2-gate:
    python3 scripts/m2_portability_gate.py
```

and change `ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check test` to `ci: lint type lock-check lint-shell lint-workflows check-mermaid docs-check m2-gate test`.

- [ ] **Step 2: Add the CI job** (memory: hosted CI invokes recipes individually — the job must be explicit in ci.yml):

```yaml
  m2-portability:
    name: M2 portability gate
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
        with:
          # The gate diffs against the pre-M2 tag, so it needs full history + tags.
          fetch-depth: 0
          persist-credentials: false

      - name: Run the portability gate (ADR-0076)
        # Stdlib-only script; no uv sync needed.
        run: python3 scripts/m2_portability_gate.py
```

- [ ] **Step 3: Lint the workflow + scripts** — `just lint-workflows` (actionlint + zizmor) and `just lint` — Expected: clean
- [ ] **Step 4: Run the gate locally once more** — `just m2-gate` — Expected: exit 0
- [ ] **Step 5: Commit** — `git commit -m "ci: run the M2 portability gate per PR"`

### Task 10: full guardrails

- [ ] **Step 1: Run the suite** — `just lint && just type && just test` — Expected: clean, zero warnings
- [ ] **Step 2: Run `just docs-check`** — Expected: no drift (M2 adds no tools)
- [ ] **Step 3: Run `just lint-shell lint-workflows check-mermaid`** — Expected: clean
- [ ] **Step 4: Fix anything red, amend nothing — separate fixup commits if needed**

---

## Self-review checklist (run after writing code)

- Spec coverage: enum+0020 (Task 1/7), package skeleton + ports (3–7), TLS factory + mutual TLS + `no_verify` forbidden (4), pkipath lifecycle on every exit path (4), `presign_get` (2), opt-in composition + discovery→capabilities jsonb (5/7), per-PR CI gate incl. running on this PR (8/9). Acceptance: parity test (7), byte-identical local-libvirt (no local_libvirt file touched — verify with `git diff main..HEAD --stat | grep local_libvirt` → empty), injected-factory TLS tests (4/5), gate passes on this PR (9).
- The parity invariant: migration 0020 and composition registration are one commit (Task 7).
- No placeholder steps; every code step shows the code.
