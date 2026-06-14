# Remote build-host (SSH target) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a server-lane kernel build dispatch its checkout + `make` to an admin-registered SSH build host (instead of the worker), ingesting the same `kernel_ref`/`debuginfo_ref`/`build_id` back into the `runs` ledger.

**Architecture:** A new `BuildTransport` port (local + ssh) slots under the existing `BuildHostOrchestrator`, so the kdump preflight and `BuildOutput`/ledger contract are unchanged. A DB-backed `build_hosts` inventory + `build_host_leases` table selects the builder and enforces fail-fast per-host capacity, admitted synchronously at the `runs.build` boundary. Local builds stay ungated. See [spec](../specs/2026-06-13-remote-build-host-design.md) and [ADR-0099](../../adr/0099-remote-build-host-targets.md).

**Tech Stack:** Python 3.13, `uv`, FastMCP, psycopg (async), Postgres, S3 (MinIO), pytest. Guardrails: `just lint`, `just type`, `just test`, `just ci`.

**Conventions every task must follow** (`CLAUDE.md` / `AGENTS.md`):
- Absolute imports only; ≤100 lines/function, complexity ≤8; ≤100-char lines; Google-style docstrings on public APIs.
- Errors: raise `CategorizedError` with the most specific `ErrorCategory`; never invent strings.
- Tools return a `ToolResponse` (`mcp/responses.py`) with `suggested_next_actions` = literal tool names.
- Secrets resolve by reference at the worker boundary, register into the redaction registry, never persist key bytes; all remote output passes the redactor before persist/response.
- TDD: write the failing test first, confirm it fails for the right reason, minimal impl, rerun, commit one logical change. Commit subject ≤72 chars, imperative, ending with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Run `just lint && just type && uv run python -m pytest <touched tests> -q` before every commit; run full `just ci` before the final push.

**Guardrail commands (exact):**
- Focused test: `uv run python -m pytest tests/<path>::<name> -q`
- Lint: `just lint`  ·  Types: `just type`  ·  Suite: `just test`  ·  Full gate: `just ci`

---

## File structure

Created:
- `src/kdive/db/schema/0027_build_hosts.sql` — `build_hosts` + `build_host_leases` + seed.
- `src/kdive/providers/build_host/transport.py` — `BuildTransport` protocol + `CommandResult`/`PresignedUpload` + `LocalBuildTransport`.
- `src/kdive/providers/build_host/ssh_transport.py` — `SshBuildTransport`.
- `src/kdive/db/build_hosts.py` — `BuildHost`/`BuildHostLease` row models + repository (resolve/list/count/acquire/release/reclaim).
- `src/kdive/services/runs/build_host_selection.py` — selection + lease admission used by `runs.build`.
- `src/kdive/mcp/tools/ops/build_hosts/__init__.py`, `registrar.py`, `register.py`, `manage.py` — admin plane.
- `tests/...` mirrors for each.

Modified:
- `src/kdive/domain/errors.py` — add `CAPACITY_EXHAUSTED`.
- `src/kdive/cli/errors.py` — map `capacity_exhausted` → 6.
- `src/kdive/db/locks.py` — add `LockScope.BUILD_HOST`.
- `src/kdive/profiles/build.py` — `build_host` field + structured `kernel_source_ref`.
- `src/kdive/providers/build_host/orchestration.py` / `workspace.py` / `execution.py` — transport-backed seams + ship patch bytes.
- `src/kdive/providers/local_libvirt/build.py`, `src/kdive/providers/remote_libvirt/build.py` — build over `LocalBuildTransport`.
- `src/kdive/mcp/tools/lifecycle/runs/build.py` — tool-boundary selection + lease.
- `src/kdive/jobs/handlers/runs.py` — lease release; transport selection.
- `src/kdive/reconciler/` — build-host lease reclaim + reachability.
- `src/kdive/mcp/app.py` — register the ops build-hosts plane.

---

## Task 1: Migration 0027 — `build_hosts` + `build_host_leases` + seed

**Files:**
- Create: `src/kdive/db/schema/0027_build_hosts.sql`
- Test: `tests/db/test_build_hosts_migration.py`

- [ ] **Step 1: Write the failing schema test** (mirror `tests/db/test_image_catalog_migration.py`).

```python
import pytest
from kdive.db.migrate import apply_migrations
from tests.db.conftest import migrated_pool  # existing fixture pattern

pytestmark = pytest.mark.usefixtures("require_docker")

async def test_build_hosts_seed_and_lease_fk(migrated_conn):
    row = await (await migrated_conn.execute(
        "SELECT kind, enabled, state FROM build_hosts WHERE name = 'worker-local'"
    )).fetchone()
    assert row == ("local", True, "ready")

async def test_build_host_leases_fk_restrict(migrated_conn):
    # inserting a lease for a missing host violates the FK
    with pytest.raises(Exception):
        await migrated_conn.execute(
            "INSERT INTO build_host_leases (run_id, build_host_id) "
            "VALUES (gen_random_uuid(), gen_random_uuid())"
        )
```

(Confirm the exact existing fixture names with `rg -n "def migrated" tests/db/conftest.py` and match them.)

- [ ] **Step 2: Run it — expect failure** (`relation "build_hosts" does not exist`).

Run: `uv run python -m pytest tests/db/test_build_hosts_migration.py -q`

- [ ] **Step 3: Write the migration.**

```sql
-- 0027_build_hosts.sql — Remote build-host inventory + capacity leases (ADR-0099).
-- Additive, forward-only (ADR-0015). build_hosts is the selection seam; build_host_leases
-- is the per-in-flight-build capacity record (rows counted under the BUILD_HOST advisory
-- lock — this codebase models capacity by counting rows, not an integer column).

CREATE TABLE build_hosts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text UNIQUE NOT NULL,
    kind               text NOT NULL CONSTRAINT build_hosts_kind_check
                       CHECK (kind IN ('local', 'ssh')),
    address            text,
    ssh_credential_ref text,
    workspace_root     text NOT NULL,
    max_concurrent     integer NOT NULL CONSTRAINT build_hosts_capacity_check
                       CHECK (max_concurrent > 0),
    enabled            boolean NOT NULL DEFAULT true,
    state              text NOT NULL DEFAULT 'ready' CONSTRAINT build_hosts_state_check
                       CHECK (state IN ('ready', 'unreachable')),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- ssh hosts need an address + credential; local hosts must omit both.
    CONSTRAINT build_hosts_ssh_fields_check CHECK (
        (kind = 'ssh'  AND address IS NOT NULL AND ssh_credential_ref IS NOT NULL) OR
        (kind = 'local' AND address IS NULL AND ssh_credential_ref IS NULL)
    )
);
CREATE TRIGGER build_hosts_set_updated_at BEFORE UPDATE ON build_hosts
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE build_host_leases (
    run_id        uuid PRIMARY KEY,
    build_host_id uuid NOT NULL REFERENCES build_hosts(id) ON DELETE RESTRICT,
    acquired_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX build_host_leases_by_host ON build_host_leases (build_host_id);

-- Seed the default local fallback. Fixed UUID so the row is identifiable/protected in code.
-- max_concurrent is informational for the local row (local builds acquire no lease).
INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent)
VALUES ('00000000-0000-0000-0000-0000000000c0', 'worker-local', 'local',
        '/var/lib/kdive/build', 1000);
```

(Verify `set_updated_at()` exists — `rg -n "set_updated_at" src/kdive/db/schema/*.sql | head` — it is used by 0013; reuse it.)

- [ ] **Step 4: Run the test — expect PASS.** `uv run python -m pytest tests/db/test_build_hosts_migration.py -q`
- [ ] **Step 5: Commit.** `feat(build-hosts): add build_hosts + build_host_leases schema (0027)`

---

## Task 2: `CAPACITY_EXHAUSTED` error category + exit code

**Files:**
- Modify: `src/kdive/domain/errors.py:51`
- Modify: `src/kdive/cli/errors.py:11-16`
- Test: `tests/domain/test_errors.py` (create if absent), `tests/cli/test_errors.py` (extend)

- [ ] **Step 1: Write failing tests.**

```python
# tests/domain/test_errors.py
from kdive.domain.errors import ErrorCategory
def test_capacity_exhausted_value():
    assert ErrorCategory.CAPACITY_EXHAUSTED.value == "capacity_exhausted"

# tests/cli/test_errors.py (add)
from kdive.cli.errors import exit_code_for_category
def test_capacity_exhausted_exit_code():
    assert exit_code_for_category("capacity_exhausted") == 6
```

Also locate any closed-set taxonomy assertion that enumerates `ErrorCategory` (`rg -n "ErrorCategory" tests/ | rg -i "all\|set\|len\|closed"`); update it to include the new value so it stays green.

- [ ] **Step 2: Run — expect FAIL** (`AttributeError`/`!= 6`).
- [ ] **Step 3: Implement.**

In `domain/errors.py`, after `AUTHORIZATION_DENIED`:
```python
    CAPACITY_EXHAUSTED = "capacity_exhausted"
```
In `cli/errors.py` `_CODES`:
```python
    "capacity_exhausted": 6,
```

- [ ] **Step 4: Run — expect PASS.** Also grep the generated taxonomy docs: `rg -n "authorization_denied" docs/guide/reference` to see if a category list is generated; if so run `just docs` (Task 13) to regenerate.
- [ ] **Step 5: Commit.** `feat(errors): add capacity_exhausted category + exit code 6`

---

## Task 3: `LockScope.BUILD_HOST`

**Files:**
- Modify: `src/kdive/db/locks.py:44-49`
- Test: `tests/db/test_locks.py` (extend; confirm name with `rg -n "LockScope" tests/`)

- [ ] **Step 1: Failing test.**
```python
from kdive.db.locks import LockScope
def test_build_host_scope():
    assert LockScope.BUILD_HOST.value == "build_host"
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — add `BUILD_HOST = "build_host"` to `LockScope`. **It is co-held with `RUN`:** Task 10 acquires it inside `runs.build`'s existing `advisory_xact_lock(conn, LockScope.RUN, run.id)` transaction (`mcp/tools/lifecycle/runs/build.py:_build_locked`). Extend the `LockScope` docstring's global total order to make `BUILD_HOST` the **last** scope: `PROJECT → RESOURCE → ALLOCATION → SYSTEM → INVESTIGATION → RUN → BUILD_HOST`. Every co-hold must take RUN before BUILD_HOST; nothing may take BUILD_HOST then RUN. (Do not call it a "never co-held" leaf — that is false.)
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.** `feat(locks): add BUILD_HOST scope after RUN in the lock order`

---

## Task 4: `build_hosts` repository — resolve / count / acquire / release / reclaim

**Files:**
- Create: `src/kdive/db/build_hosts.py`
- Test: `tests/db/test_build_hosts_repo.py`

Row models + functions (async, take `AsyncConnection`):

```python
from dataclasses import dataclass
from uuid import UUID
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.errors import CategorizedError, ErrorCategory

WORKER_LOCAL_ID = UUID("00000000-0000-0000-0000-0000000000c0")

@dataclass(slots=True, frozen=True)
class BuildHost:
    id: UUID
    name: str
    kind: str            # 'local' | 'ssh'
    address: str | None
    ssh_credential_ref: str | None
    workspace_root: str
    max_concurrent: int
    enabled: bool
    state: str

async def get_by_name(conn: AsyncConnection, name: str) -> BuildHost | None: ...
async def lease_count(conn: AsyncConnection, host_id: UUID) -> int: ...

async def try_acquire_lease(conn: AsyncConnection, host: BuildHost, run_id: UUID) -> bool:
    """Acquire one capacity lease for run_id under the BUILD_HOST lock. Caller is INSIDE a
    transaction (so the lease + the BUILD-job enqueue commit atomically). Returns False when
    the host is full. Idempotent: a re-acquire for the same run_id is a no-op True."""
    await advisory_xact_lock(conn, LockScope.BUILD_HOST, host.id)
    # idempotent re-acquire
    existing = await (await conn.execute(
        "SELECT 1 FROM build_host_leases WHERE run_id = %s", (run_id,))).fetchone()
    if existing is not None:
        return True
    count = await lease_count(conn, host.id)
    if count >= host.max_concurrent:
        return False
    await conn.execute(
        "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
        (run_id, host.id))
    return True

async def release_lease(conn: AsyncConnection, run_id: UUID) -> None:
    await conn.execute("DELETE FROM build_host_leases WHERE run_id = %s", (run_id,))
```

- [ ] **Step 1: Failing tests** (against `migrated_conn`): `get_by_name('worker-local')` returns kind local; register an ssh host row directly, `try_acquire_lease` up to `max_concurrent` then returns False; re-acquire same run_id → True and count unchanged; `release_lease` is idempotent (call twice).
- [ ] **Step 2: Run — expect FAIL** (module missing).
- [ ] **Step 3: Implement** the module.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.** `feat(build-hosts): add repository with lease acquire/release/count`

---

## Task 5: `BuildTransport` port + `LocalBuildTransport` (behavior-preserving refactor)

**Files:**
- Create: `src/kdive/providers/build_host/transport.py`
- Test: `tests/providers/build_host/test_local_transport.py`

Define the port and the local realization that wraps today's primitives **without changing behavior**:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

@dataclass(slots=True, frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

@dataclass(slots=True, frozen=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]

class BuildTransport(Protocol):
    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult: ...
    def read_text(self, path: str) -> str: ...
    def read_bytes(self, path: str) -> bytes: ...                 # small files (.config, build-id note)
    def write_bytes(self, path: str, data: bytes) -> None: ...    # ship fragment/patch bytes
    def clone(self, remote: str, ref: str, dest: str) -> None: ...
    def upload_file(self, path: str, presigned: PresignedUpload) -> str: ...  # host PUTs the
        # (possibly large) artifact straight to S3 (worker never holds the bytes — ADR-0099
        # decision 6); returns the stored object's etag/checksum. Local impl reads the file and
        # PUTs via the worker object store; ssh impl runs `curl -T <path> <url>` on the host.
    def cleanup(self, path: str) -> None: ...

class LocalBuildTransport:
    """Today's behavior behind the port: fixed-argv subprocess + local file IO."""
    def run(self, argv, *, cwd, timeout_s):  # mirrors run_make_target's subprocess.run
        ...
    def read_text(self, path): return Path(path).read_text()
    ...
    def clone(self, remote, ref, dest):
        raise CategorizedError("git provenance is not valid for a local build host",
                               category=ErrorCategory.CONFIGURATION_ERROR)
```

**Behavior-preservation guard (the back-compat test):** assert `LocalBuildTransport.run(...)` issues the identical argv/timeout/`check=False` call as the pre-refactor `run_make_target` — drive it with a fake `subprocess.run` (monkeypatch) and assert the captured argv equals `["make", "-C", cwd, ...]`. Do **not** assert a golden `BuildOutput` (keys are per-run).

- [ ] **Step 1: Failing tests** — argv-capture for `run`; `clone` raises `configuration_error`; `read_text`/`read_bytes`/`write_bytes` round-trip a tmp file; `upload_file` reads a tmp file and PUTs it via an injected fake object store (returns the fake etag).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** `transport.py`.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.** `feat(build-hosts): add BuildTransport port + LocalBuildTransport`

---

## Task 6: `SshBuildTransport`

**Files:**
- Create: `src/kdive/providers/build_host/ssh_transport.py`
- Test: `tests/providers/build_host/test_ssh_transport.py`

Constructor takes `(address, identity_path, secret_registry)`. `run` builds a fixed argv:
`["ssh", "-i", identity_path, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", address, "--", *argv]` and shells out via `subprocess.run` with `timeout_s`; map launch/timeout faults via the existing `launch_failure`. `read_*`/`write_bytes` use `sftp`/`scp` with fixed argv. `clone` uses the init+fetch model:

```python
def clone(self, remote: str, ref: str, dest: str) -> None:
    self.run(["git", "init", dest], cwd="/", timeout_s=GIT_TIMEOUT)
    self.run(["git", "-C", dest, "fetch", "--depth", "1", remote, ref], cwd="/", timeout_s=GIT_TIMEOUT)
    res = self.run(["git", "-C", dest, "checkout", "FETCH_HEAD"], cwd="/", timeout_s=GIT_TIMEOUT)
    if res.returncode != 0:
        raise CategorizedError("git ref could not be checked out on the build host",
                               category=ErrorCategory.CONFIGURATION_ERROR,
                               details={"stderr": redacted_tail(res.stderr)})
```

Argv is fixed (no shell), so remote/ref shape is validated (reject control chars / leading `-`) before use. All returned stderr passes `redacted_tail` before it enters any `CategorizedError.details`.

**This task owns credential materialization** (spec §7.3) — do not defer it. Add a context
manager `materialized_ssh_identity(ssh_credential_ref, secret_registry)` that: (1) resolves the
secret-ref to key bytes at the worker boundary; (2) `secret_registry.register(...)` the bytes
so the redactor scrubs them from all output; (3) writes a `0600` temp identity file (mirror
`providers/remote_libvirt/transport.py:materialized_pkipath` / `_write_private`); (4) `yield`s
the path; (5) `unlink`s it in `finally`. `SshBuildTransport.from_host(host, secret_registry)`
enters this CM for the duration of a build and passes the path as `-i`.

- [ ] **Step 1: Failing tests** — monkeypatch `subprocess.run`; assert `run` argv is the ssh wrapper with `-i identity` and `--` separator; assert `clone` issues init→fetch→checkout in order; assert a non-zero checkout raises `configuration_error` with redacted stderr; assert a remote/ref containing a shell metacharacter or leading `-` is rejected (`configuration_error`) before any subprocess call; assert `materialized_ssh_identity` writes a 0600 file, registers the bytes with the `SecretRegistry`, and `unlink`s the file on exit including when the body raises.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** `ssh_transport.py` + `materialized_ssh_identity`.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.** `feat(build-hosts): add SshBuildTransport + materialized SSH identity`

---

## Task 7: Transport-backed seams wired through `BuildHostOrchestrator`

**Files:**
- Modify: `src/kdive/providers/build_host/workspace.py` (transport-backed checkout: ship fragment + patch bytes, run merge/defconfig over transport)
- Modify: `src/kdive/providers/build_host/execution.py` (transport-backed `RunStep`/`ReadConfig` factories)
- Modify: `src/kdive/providers/build_host/orchestration.py` (no contract change; only seam construction site)
- Test: `tests/providers/build_host/test_transport_seams.py`

Add factory helpers that build the orchestrator seams from a `BuildTransport`, e.g.:
```python
def transport_run_step(t: BuildTransport, args: list[str], timeout_s: int) -> RunStep:
    def _step(ws: Path) -> int:
        return t.run(["make", "-C", str(ws), *args], cwd=str(ws), timeout_s=timeout_s).returncode
    return _step

def transport_read_config(t: BuildTransport) -> ReadConfig:
    def _read(ws: Path) -> str:
        return t.read_text(str(ws / ".config"))
    return _read
```
The transport-backed `Checkout` writes the config fragment and (when present) the resolved patch bytes to the workspace via `t.write_bytes`, runs `git apply` over `t.run`, and applies the same silent-skip guards by reading target files back via `t.read_bytes` before/after. The worker still runs `_validate_final_config` on the read-back `.config` (unchanged).

- [ ] **Step 1: Failing tests** — a fake `BuildTransport` records calls; assert the orchestrator drives checkout→olddefconfig→read_config→make as the same call sequence for a fake-local and fake-ssh transport, and that `_validate_final_config` runs on the worker against the transport's read-back `.config`; assert a `patch_ref` causes the patch bytes to be written via the transport and `git apply` to run over it.
- [ ] **Step 2–4: Run/implement/pass.**
- [ ] **Step 5: Commit.** `feat(build-hosts): wire transport-backed orchestrator seams`

---

## Task 7.5: Remote artifact pipeline + presigned publish (the post-make half)

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/build.py` (`RemoteLibvirtBuild.build` post-make steps `modules_install` → build-id → bundle → vmlinux → publish)
- Modify: `src/kdive/providers/local_libvirt/build.py` (route its post-make `_put` through `transport.upload_file` too, so `local_libvirt` + ssh-host works; for `LocalBuildTransport` this is unchanged worker-PUT behavior)
- Modify: `src/kdive/providers/build_host/execution.py` (transport-backed `run_modules_install`, build-id note extraction, bundle packaging)
- Test: `tests/providers/remote_libvirt/test_build_transport_publish.py`, `tests/providers/local_libvirt/test_build_transport_publish.py`

**Builder ⟂ build-host.** The selected build host is independent of the run's provider:
either provider's builder may run on an ssh host. So **both** builders' publish step routes
through `transport.upload_file` (presigned for ssh, worker-PUT for local). The modules-bundle
packaging below is `remote_libvirt`-specific (local has no modules tree); `local_libvirt`'s
simpler bzImage+vmlinux publish takes the same `upload_file` swap without the bundle step.

**Why this task exists:** `RemoteLibvirtBuild.build` (build.py:148-159) does not stop at `make`.
After `make` it runs, today on the worker against local workspace files: `run_modules_install`
→ `read_build_id` (objcopy on `vmlinux`) → `_build_bundle` (tar of `boot/vmlinuz` + `lib/modules`)
→ `read_vmlinux` → `_put` (worker object store). For an SSH host the workspace is **remote**, so
each of these must run over the transport and the large artifacts must publish per ADR-0099
decision 6 (host PUTs to S3; worker never holds the bytes). This task remotes that half.

Concrete remoting:
- **modules_install:** `transport.run(["make","-C",ws,f"INSTALL_MOD_PATH={mod_root}","modules_install"], ...)` into a remote `mod_root`.
- **build-id:** run `objcopy -O binary --only-section=.notes <ws>/vmlinux <note>` over the transport, then `transport.read_bytes(<note>)` (small) back to the worker and `parse_gnu_build_id` on the worker (unchanged parser).
- **bundle:** package on the host (files are remote) — `transport.run(["tar","-C",mod_root,...,"-czf",bundle_path, ...])` plus including `boot/vmlinuz`; keep the existing back-reference-symlink exclusion (`build`/`source`). Produces a remote `bundle_path`.
- **publish:** for each of `bundle_path` and `<ws>/vmlinux`, the worker mints a `PresignedUpload` (object-store presign for the run-scoped key, `Sensitivity.SENSITIVE`, retention `build`), then `transport.upload_file(remote_path, presigned)`; record the returned key/etag as `kernel_ref`/`debuginfo_ref`. `BuildOutput` is unchanged.
- For `LocalBuildTransport`, `upload_file` reads the local file and PUTs via the worker object store — i.e. today's `_put` behavior, so the local path is byte-for-byte unchanged.

- [ ] **Step 1: Failing tests** — with a fake ssh transport: assert modules_install/objcopy/tar run over `transport.run` against the remote workspace; assert the build-id note is `read_bytes` back and parsed on the worker; assert `kernel_ref`/`debuginfo_ref` come from `transport.upload_file` (presigned), not a worker-side `_put`, and the worker never reads the bundle bytes. With `LocalBuildTransport`: assert the publish path still PUTs via the worker object store (unchanged local behavior).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the post-make remoting + presigned publish.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit.** `feat(build-hosts): remote the post-make artifact pipeline + presigned publish`

---

## Task 8: `ServerBuildProfile.build_host` + structured `kernel_source_ref`

**Files:**
- Modify: `src/kdive/profiles/build.py:65-77`
- Test: `tests/profiles/test_build_profile.py` (confirm name with `rg -n "ServerBuildProfile" tests/`)

Add an optional `build_host: NonEmptyStr | None = None`. Change `kernel_source_ref` to accept either a string or a git object:

```python
class GitSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    remote: NonEmptyStr
    ref: NonEmptyStr

class ServerBuildProfile(_BuildProfileBase):
    source: Literal["server"] = "server"
    kernel_source_ref: NonEmptyStr | _GitSourceWrapper   # {"git": {...}} | "path-string"
    build_host: NonEmptyStr | None = None
    config: ComponentRef | None = None
    profile_requirements: ProfileRequirementsRef | None = None
    patch_ref: NonEmptyStr | None = None
```

Provenance helpers (used by selection in Task 10): `is_git_source(profile) -> bool`. The fail-closed cross-check (local+git, ssh+string) lives at the `runs.build` boundary (Task 10), not in the parser — the parser only validates shape. Keep `dump_build_profile` round-tripping both forms.

- [ ] **Step 1: Failing tests** — parse a string `kernel_source_ref` (warm-tree) and a `{git:{remote,ref}}` form; reject `{git:{}}` (missing fields) as `configuration_error`; `build_host` optional, rejects empty string; round-trip via `dump_build_profile`.
- [ ] **Step 2–4: Run/implement/pass.**
- [ ] **Step 5: Commit.** `feat(build): add build_host + structured kernel_source_ref to profile`

---

## Task 9: `build_hosts.*` admin plane

**Files:**
- Create: `src/kdive/mcp/tools/ops/build_hosts/{__init__.py,registrar.py,register.py,manage.py}`
- Modify: `src/kdive/mcp/app.py` (add `_register_ops_build_hosts_tools` to `_PLANE_REGISTRARS`)
- Test: `tests/mcp/tools/ops/test_build_hosts.py`

Mirror an existing ops plane (read `src/kdive/mcp/tools/ops/images/registrar.py` and a mutation handler for the exact `require_role`/`audit.record`/`ToolResponse` shape). Handlers are plain async funcs taking `(pool, ctx, ...)`:

- `build_hosts.register(name, address, ssh_credential_ref, workspace_root, max_concurrent)` — `require_role(ctx, Role.PLATFORM_ADMIN)`. **Only `kind='ssh'` is registerable**; `kind='local'` is rejected with `configuration_error` ("local builds run on the worker; the built-in `worker-local` host is the only local row"). `address` + `ssh_credential_ref` are required (the CHECK enforces it too). Validate `ssh_credential_ref` resolves by reference (presence only — do not fetch bytes); INSERT with `kind='ssh'`; `audit.record(... tool="build_hosts.register" ...)`; return `ToolResponse` with the new id and `suggested_next_actions=["build_hosts.list","runs.build"]`. Duplicate name → `conflict`.
- `build_hosts.list()` — read-only passthrough; returns rows (no secret bytes; `ssh_credential_ref` shown as the ref string only).
- `build_hosts.disable(name)` — PLATFORM_ADMIN; refuse `worker-local` → `conflict`; set `enabled=false`; audit.
- `build_hosts.remove(name)` — PLATFORM_ADMIN; refuse `worker-local` → `conflict`; refuse if a `build_host_leases` row references it → `conflict` (also FK-enforced); DELETE; audit.

- [ ] **Step 1: Failing tests** — non-admin → `authorization_denied`; `register kind='local'` → `configuration_error`; register ssh host then `list` shows it with only the credential *ref* (no bytes); register duplicate name → `conflict`; `disable`/`remove` of `worker-local` → `conflict`; `remove` with an outstanding lease → `conflict`; audit row written with `(principal, ...)`; the persisted row and any error `details` contain no secret bytes (no-leak guard).
- [ ] **Step 2–4: Run/implement/pass** (register `_register_ops_build_hosts_tools` in `app.py` `_PLANE_REGISTRARS`).
- [ ] **Step 5: Commit.** `feat(build-hosts): add build_hosts admin plane (register/list/disable/remove)`

---

## Task 10: `runs.build` tool-boundary selection + lease admission

**Files:**
- Modify: `src/kdive/jobs/payloads.py` (add `build_host_id` to `BuildPayload`)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/build.py`
- Create: `src/kdive/services/runs/build_host_selection.py`
- Test: `tests/mcp/tools/lifecycle/test_runs_build_selection.py`

**Step 0 — add the handoff field.** Extend `BuildPayload` (today: `run_id`, `cmdline`) with
`build_host_id: str | None = None` (a UUID string; validate shape like `run_id`). This is the
field Task 11's handler and Task 12's reconciler read; without it the handler cannot know which
host the lease is held against. (Confirm the model with `rg -n "class BuildPayload" src/kdive/jobs/payloads.py`.)

`build_host_selection.resolve_and_admit(conn, profile, run_id)`:
1. Parse the build profile (existing). Resolve host: `get_by_name(profile.build_host or 'worker-local')`. Absent → `not_found`. `enabled=false` or `state='unreachable'` → `configuration_error`.
2. Provenance cross-check: `host.kind=='local'` and `is_git_source(profile)` → `configuration_error`; `host.kind=='ssh'` and not git source → `configuration_error`.
3. If `host.kind != 'local'`: `try_acquire_lease(conn, host, run_id)`; False → raise `CategorizedError(category=CAPACITY_EXHAUSTED, ...)`.
4. Return the resolved `host` (id + kind). The caller puts `build_host_id=str(host.id)` into the `BuildPayload` and enqueues the BUILD job **in the same transaction** as the lease insert.

The tool already runs inside `_build_locked`'s `advisory_xact_lock(LockScope.RUN, run.id)`
transaction (`build.py:_build_locked`). Acquire `LockScope.BUILD_HOST` **after** RUN inside
that same transaction (the order fixed in Task 3) and enqueue there, so lease+payload+enqueue
commit atomically (spec §6). On `capacity_exhausted`, return a failure `ToolResponse` with
`suggested_next_actions=["runs.build"]` (retry).

- [ ] **Step 1: Failing tests** (drive the handler directly with an injected pool):
  - no `build_host` → resolves `worker-local`, no lease row created, BUILD job enqueued.
  - named-but-absent → `not_found`.
  - named-but-disabled → `configuration_error`.
  - ssh host + warm-tree string → `configuration_error`; local host + git → `configuration_error`.
  - ssh host at capacity (pre-fill leases to `max_concurrent`) → `capacity_exhausted`, no job enqueued.
  - ssh host with a slot → lease row created **and** BUILD job enqueued atomically; simulate a failure after acquire (raise before enqueue) → assert neither lease nor job persisted (atomicity).
- [ ] **Step 2–4: Run/implement/pass.**
- [ ] **Step 5: Commit.** `feat(runs): select build host + admit capacity at runs.build boundary`

---

## Task 11: build handler — transport selection + lease release

**Files:**
- Modify: `src/kdive/jobs/handlers/runs.py:102-140` (`build_handler`)
- Modify: provider builders to accept a transport (`local_libvirt/build.py`, `remote_libvirt/build.py`) or resolve it from the recorded `build_host` id.
- Test: `tests/jobs/handlers/test_build_handler_transport.py`

The handler reads `payload.build_host_id` (Task 10's field; falls back to `worker-local` when null, for back-compat with already-queued jobs), loads the `build_hosts` row, builds the matching transport (`LocalBuildTransport` for kind local; `SshBuildTransport.from_host(host, secret_registry)` — entering the materialized-identity CM from Task 6 — for kind ssh), runs `builder.build` on it, and **always** releases the lease in a `finally` (`release_lease(conn, run_id)` — idempotent, runs on success and on every failure path, including the existing `_fail_build`). Local builds have no lease; `release_lease` is a harmless no-op DELETE.

- [ ] **Step 1: Failing tests** — a local-host run uses `LocalBuildTransport` and never touches leases; an ssh-host run builds the ssh transport from the row and, on both success and a raised `CategorizedError`, the lease row is deleted; a worker-death simulation (handler raises before release) leaves the lease for the reconciler (Task 12).
- [ ] **Step 2–4: Run/implement/pass.**
- [ ] **Step 5: Commit.** `feat(runs): build over selected transport + release build-host lease`

---

## Task 12: reconciler — lease reclaim by job-liveness + reachability

**Files:**
- Create/Modify: `src/kdive/reconciler/build_hosts.py` (+ wire into the reconciler loop alongside `allocations.py`)
- Test: `tests/reconciler/test_build_hosts.py`

- **Reclaim:** delete a lease whose owning BUILD job is terminal or gone. The jobs table stores `run_id` **inside the JSONB payload**, not as a top-level column, so the predicate is a JSONB extract, not a column join:
  ```sql
  DELETE FROM build_host_leases l
  WHERE NOT EXISTS (
      SELECT 1 FROM jobs j
      WHERE j.kind = 'build'
        AND (j.payload->>'run_id')::uuid = l.run_id
        AND j.state IN (<live states>)   -- e.g. claimable/running; NOT succeeded/failed
  );
  ```
  Confirm the exact jobs table name, payload column, kind value, and live-state set with `rg -n "class Job\b|JobState|JobKind|payload|state" src/kdive/domain/models.py src/kdive/jobs/queue.py src/kdive/db/schema/*.sql`. The payload extract is unindexed; scope the scan to existing leases (the `build_host_leases` table is small), not a full jobs scan. **Never** reclaim by age.
- **Health:** for each `kind='ssh'` host, probe `ssh ... true` via the transport (bounded timeout); set `state='unreachable'` on failure, `'ready'` on success. Do not delete leases on unreachable.

- [ ] **Step 1: Failing tests** — a lease whose BUILD job is `failed`/absent is reclaimed; a lease whose BUILD job is live (running/claimable) is **kept**; a probe failure flips `state` to `unreachable` and a subsequent selection (Task 10) rejects it; a probe success flips back to `ready`.
- [ ] **Step 2–4: Run/implement/pass.**
- [ ] **Step 5: Commit.** `feat(reconciler): reclaim build-host leases by job liveness + health probe`

---

## Task 13: generated docs, full gate, portability gate

**Files:**
- Possibly: `docs/guide/reference/*` (regenerated), m2-gate allowlist / meta-test if core-touch trips it.

- [ ] **Step 1: Regenerate tool reference.** Run `just docs` (or the generator used by `docs-check`), review the diff for the new `build_hosts.*` tools and any taxonomy list, and commit it: `docs(reference): regenerate for build_hosts plane`.
- [ ] **Step 2: Run the full gate.** `just ci`. Fix every warning/failure before proceeding.
- [ ] **Step 3: Portability / m2 gate.** This feature touches core (`runs` tool, build handler, profile, errors, locks, app.py). If `just ci` includes a portability/core-touch gate (ADR-0076) or an `m2-gate` allowlist meta-test, update the allowlist **and** its meta-test together (see memory: the gate is enforced in `ci.yml`, and a guard added only to the `ci` recipe won't gate — update the workflow-invoked checks). Record the rationale in the commit.
- [ ] **Step 4: Commit** any generated/allowlist changes: `chore: regenerate docs + update gates for build_hosts`

---

## Self-review (run before handoff)

- **Spec coverage:** §4 seam → Tasks 5–7,7.5,11; artifact-return path (ADR-0099 decision 6) → Tasks 5 (`upload_file`),7.5; §5 inventory → Tasks 1,4,9; §6 capacity → Tasks 1,4,10; §7 provenance/secrets → Tasks 6,8,10; §8 reconciler → Task 12; §9 scope → all; §10 testing → every task's Step 1; taxonomy → Task 2; locks (RUN→BUILD_HOST order) → Task 3.
- **Placeholders:** none — every code step shows real code or the exact `rg` to confirm a local name before matching.
- **Type consistency:** `BuildTransport`/`CommandResult`/`PresignedUpload`/`upload_file` (Task 5) are reused verbatim in Tasks 6,7,7.5,11; `BuildHost`/`try_acquire_lease`/`release_lease` (Task 4) are reused in Tasks 10–12; `is_git_source`/`build_host` (Task 8) reused in Task 10; `BuildPayload.build_host_id` (Task 10 Step 0) is read in Tasks 11–12.
- **Confirm-before-match:** several steps depend on existing names (fixtures, jobs state column, ops registrar shape, taxonomy closed-set test). Each such step names the `rg` to run first — the implementer must confirm, not assume.
