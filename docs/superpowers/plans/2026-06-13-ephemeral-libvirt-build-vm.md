# Ephemeral remote-libvirt build VM (target 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to
> implement this plan task-by-task. The tasks are tightly coupled (shared transport base →
> subclass → handler → reconciler), so they run in this session, not via parallel subagents.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a server-lane build target an ephemeral VM provisioned on demand on the
configured remote-libvirt host (`kind='ephemeral_libvirt'`), run the build over the in-guest
guest-agent exec channel, and tear the VM down — ingesting the same
`kernel_ref`/`debuginfo_ref`/`build_id` as the SSH target.

**Architecture:** A `GuestExecBuildTransport` (over `GuestAgentExec`, one `sh -c` hop like the
SSH transport) is a new `BuildTransport` realization under the existing
`RemoteLibvirtBuild.over_transport` / `BuildHostOrchestrator` — the build, `BuildOutput`,
ledger, capacity/lease seam, and error taxonomy are unchanged. A bare provider-managed
`kdive-build-<run_id>` libvirt domain (qcow2 overlay over an operator-staged base build image,
no gdbstub) is provisioned/torn-down by the BUILD handler and reaped by the reconciler via a
name marker + owning-BUILD-job liveness. See
[spec](../specs/2026-06-13-ephemeral-libvirt-build-vm.md) and
[ADR-0100](../../adr/0100-ephemeral-libvirt-build-vm.md).

**Tech Stack:** Python 3.13, `uv`, FastMCP, psycopg (async), Postgres, S3 (MinIO), libvirt
(`qemu+tls://`), qemu-guest-agent, pytest. Guardrails: `just lint`, `just type`, `just test`,
`just ci`.

**Conventions every task must follow** (`CLAUDE.md` / `AGENTS.md`):
- Absolute imports only; ≤100 lines/function, complexity ≤8; ≤100-char lines; Google-style
  docstrings on public APIs.
- Errors: raise `CategorizedError` with the most specific existing `ErrorCategory`; never
  invent strings. No new taxonomy value (PROVISIONING_FAILURE / TRANSPORT_FAILURE /
  BUILD_FAILURE / CONFIGURATION_ERROR / CAPACITY_EXHAUSTED already cover this).
- Tools return a `ToolResponse` with `suggested_next_actions` = literal tool names.
- Secrets resolve by reference at the worker boundary, register into the redaction registry,
  never persist key bytes; all remote output passes the redactor before persist/response.
- `just type` is whole-tree; the unstubbed C-ext deps (`libvirt`, `libvirt_qemu`, `drgn`) use a
  scoped per-site `# ty: ignore[unresolved-import]` only where already established — do not add
  blanket ignores.
- TDD: write the failing test first, confirm it fails for the right reason, minimal impl,
  rerun, refactor green. Commit one logical change; subject ≤72 chars, imperative, ending with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Before every commit: `just lint && just type && uv run python -m pytest <touched tests> -q`.
  Before the final push: full `just ci`.

**Guardrail commands (exact):**
- Focused test: `uv run python -m pytest tests/<path>::<name> -q`
- Lint: `just lint`  ·  Types: `just type`  ·  Suite: `just test`  ·  Full gate: `just ci`

---

## File structure

**Created:**
- `src/kdive/db/schema/0029_ephemeral_libvirt_build_host.sql` — widen `kind` CHECK, add
  `base_image_volume`, replace the per-kind field CHECK.
- `src/kdive/providers/build_host/shell_transport.py` — `ShellBuildTransport` base.
- `src/kdive/providers/build_host/guest_exec_transport.py` — `GuestExecBuildTransport`.
- `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py` — `EphemeralBuildVm`
  (provision/teardown `session` context manager) + `render_build_domain_xml`.
- `src/kdive/providers/remote_libvirt/build_vm_reaper.py` — `BuildVm` row +
  `RemoteLibvirtBuildVmReaper` (libvirt list/delete seam) + `build_vm_name`/`run_id_from_*`.
- Tests mirroring each under `tests/`.

**Modified:**
- `src/kdive/db/build_hosts.py` — `BuildHost.base_image_volume` field + `_row_to_host`.
- `src/kdive/services/runs/build_host_selection.py` — provenance cross-check `kind != 'local'`.
- `src/kdive/mcp/tools/ops/build_hosts/register.py` — optional `kind` + per-kind field
  validation + ephemeral INSERT.
- `src/kdive/mcp/tools/ops/build_hosts/registrar.py` — pass `kind`/`base_image_volume` through.
- `src/kdive/providers/build_host/ssh_transport.py` — subclass `ShellBuildTransport`
  (behavior-preserving).
- `src/kdive/jobs/handlers/runs.py` — `_run_build` ephemeral branch + `ephemeral_build_session`
  seam.
- `src/kdive/reconciler/build_hosts.py` — `reap_orphan_build_vms` (reap before reclaim).
- `src/kdive/reconciler/<loop>.py` + composition — wire the build-VM reaper, ordered before
  lease reclaim.
- `src/kdive/providers/remote_libvirt/config.py` — build-VM vcpu/mem sizing (if not already
  derivable); reuse existing network/pool/machine.

---

## Task 1: Migration 0029 — widen `build_hosts` for `ephemeral_libvirt`

**Files:**
- Create: `src/kdive/db/schema/0029_ephemeral_libvirt_build_host.sql`
- Modify: `src/kdive/db/build_hosts.py` (dataclass + `_row_to_host`)
- Test: `tests/db/test_build_hosts_migration.py` (extend; mirror existing build_hosts schema test)

- [ ] **Step 1: Write the failing schema test.** Add cases: a `kind='ephemeral_libvirt'` row
  with `base_image_volume` NOT NULL and `address`/`ssh_credential_ref` NULL INSERTs OK; the
  same row WITHOUT `base_image_volume` violates `build_hosts_fields_check`; an `ssh` row WITH
  `base_image_volume` violates it; `worker-local` seed still present and `local`-valid.

```python
async def test_ephemeral_libvirt_row_requires_base_image_volume(migrated_conn):
    await migrated_conn.execute(
        "INSERT INTO build_hosts (name, kind, base_image_volume, workspace_root, max_concurrent)"
        " VALUES ('builders', 'ephemeral_libvirt', 'kdive-build-base.qcow2', '/build', 2)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        await migrated_conn.execute(
            "INSERT INTO build_hosts (name, kind, workspace_root, max_concurrent)"
            " VALUES ('bad', 'ephemeral_libvirt', '/build', 2)"
        )
```

- [ ] **Step 2: Run it — FAIL** (`kind` CHECK rejects `ephemeral_libvirt`; column missing).
- [ ] **Step 3: Write the migration** (exact SQL from spec §5):

```sql
-- 0029_ephemeral_libvirt_build_host.sql — admit kind='ephemeral_libvirt' (ADR-0100).
-- Additive/forward-only (ADR-0015): widen the kind CHECK, add base_image_volume (the
-- operator-staged base build image volume), and replace the per-kind field CHECK so each
-- kind constrains its own columns. The build VM lives on the single configured
-- remote-libvirt host, so an ephemeral row carries no address/ssh_credential_ref.
ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_kind_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_kind_check
    CHECK (kind IN ('local', 'ssh', 'ephemeral_libvirt'));
ALTER TABLE build_hosts ADD COLUMN base_image_volume text;
ALTER TABLE build_hosts DROP CONSTRAINT build_hosts_ssh_fields_check;
ALTER TABLE build_hosts ADD CONSTRAINT build_hosts_fields_check CHECK (
    (kind = 'local'             AND address IS NULL     AND ssh_credential_ref IS NULL     AND base_image_volume IS NULL) OR
    (kind = 'ssh'               AND address IS NOT NULL AND ssh_credential_ref IS NOT NULL AND base_image_volume IS NULL) OR
    (kind = 'ephemeral_libvirt' AND address IS NULL     AND ssh_credential_ref IS NULL     AND base_image_volume IS NOT NULL)
);
```

- [ ] **Step 4: Add the dataclass field + mapping.** In `db/build_hosts.py`: add
  `base_image_volume: str | None` to `BuildHost` (after `ssh_credential_ref`, with docstring),
  and `base_image_volume=cast("str | None", row["base_image_volume"])` in `_row_to_host`.
- [ ] **Step 5: Run the migration test + the existing `build_hosts` repo tests — PASS.**
- [ ] **Step 6: Run the build_hosts migration + repo suites** (the real guardrail for this
  CHECK widen — the `build_hosts.kind` CHECK is free-text, not an `ErrorCategory`-enum-backed
  category, so `test_migrate.py`'s enum-coverage test does not apply here). Run:
  `uv run python -m pytest tests/db/test_build_hosts_migration.py tests/db/test_build_hosts_repo.py -q`.
- [ ] **Step 7: Commit** — `feat(db): admit kind='ephemeral_libvirt' in build_hosts (0029)`.

---

## Task 2: Registration — `build_hosts.register` admits `ephemeral_libvirt`

**Files:**
- Modify: `src/kdive/mcp/tools/ops/build_hosts/register.py`,
  `src/kdive/mcp/tools/ops/build_hosts/registrar.py`
- Test: `tests/mcp/tools/ops/build_hosts/test_register.py` (extend)

- [ ] **Step 1: Write failing tests.** (a) `kind='ephemeral_libvirt'` + `base_image_volume`
  inserts a row with NULL address/cred and the volume set; (b) `kind='ephemeral_libvirt'`
  without `base_image_volume` → `configuration_error`; (c) `kind='ephemeral_libvirt'` WITH
  `address` or `ssh_credential_ref` → `configuration_error`; (d) non-admin → `authorization_denied`
  (unchanged); (e) the audit row carries no secret (ephemeral has none); (f) default
  `kind='ssh'` path unchanged (existing tests stay green).
- [ ] **Step 2: Run — FAIL** (`register_build_host` has no `kind`/`base_image_volume` params).
- [ ] **Step 3: Generalize `register_build_host`.** Add params `kind: str = "ssh"` and
  `base_image_volume: str | None = None`; make `address`/`ssh_credential_ref` optional. Branch:

```python
if kind == "ssh":
    if not _validate_credential_ref(ssh_credential_ref or ""):
        return _config_error(name, "ssh_credential_ref must be a non-blank reference string")
    if not address:
        return _config_error(name, "ssh build host requires an address")
    if base_image_volume:
        return _config_error(name, "base_image_volume is not valid for an ssh build host")
    cols = ("name", "kind", "address", "ssh_credential_ref", "workspace_root", "max_concurrent")
    vals = (name, "ssh", address, ssh_credential_ref, workspace_root, max_concurrent)
elif kind == "ephemeral_libvirt":
    if not (base_image_volume and base_image_volume.strip()):
        return _config_error(name, "ephemeral_libvirt build host requires a base_image_volume")
    if address or ssh_credential_ref:
        return _config_error(name, "address/ssh_credential_ref are not valid for an "
                                   "ephemeral_libvirt build host")
    cols = ("name", "kind", "base_image_volume", "workspace_root", "max_concurrent")
    vals = (name, "ephemeral_libvirt", base_image_volume, workspace_root, max_concurrent)
else:
    return _config_error(name, f"unsupported build host kind {kind!r}")
```

  Build the parameterized INSERT from `cols`/`vals` (psycopg placeholders), keep the
  `max_concurrent <= 0` guard, the `UniqueViolation → CONFLICT`, and the audit (the audit
  `args` for an ephemeral row name `base_image_volume`, never a secret). Add a small
  `_config_error(name, reason)` helper to DRY the failures.
- [ ] **Step 4: Thread `kind`/`base_image_volume` through `registrar.py`** (the FastMCP
  wrapper): add the two optional tool params (with docstrings) and pass them to
  `register_build_host`. `kind` defaults to `"ssh"`.
- [ ] **Step 5: Run the register tests — PASS.**
- [ ] **Step 6: Regenerate the tool reference** (`just docs`) — the build_hosts.register schema
  changed; commit the regenerated `docs/guide/reference/...`.
- [ ] **Step 7: `just lint && just type && uv run python -m pytest tests/mcp/tools/ops/build_hosts -q`.**
- [ ] **Step 8: Commit** — `feat(ops): register ephemeral_libvirt build hosts`.

---

## Task 3: Selection — non-local hosts require a git source

**Files:**
- Modify: `src/kdive/services/runs/build_host_selection.py`
- Test: `tests/services/runs/test_build_host_selection.py` (extend)

- [ ] **Step 1: Write failing tests.** `ephemeral_libvirt` + warm-tree string →
  `configuration_error`; `ephemeral_libvirt` + git source → lease acquired (returns the host);
  `ephemeral_libvirt` at ceiling → `capacity_exhausted`; disabled/unreachable → `configuration_error`.
  Reuse the existing fakes; insert an `ephemeral_libvirt` host row in the fixture.
- [ ] **Step 2: Run — FAIL** (current check is `host.kind == "ssh" and not git`, so an
  `ephemeral_libvirt` + warm-tree is wrongly admitted).
- [ ] **Step 3: Generalize the cross-check.** Replace the two-branch check:

```python
    if host.kind == "local" and git:
        raise CategorizedError(
            "a local build host requires a warm-tree kernel_source_ref, not a git ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "host_kind": host.kind},
        )
    if host.kind != "local" and not git:
        raise CategorizedError(
            "a remote build host requires a git kernel_source_ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_host": name, "host_kind": host.kind},
        )
```

  The capacity gate already keys on `host.kind != "local"`, so `ephemeral_libvirt` acquires a
  lease with no further change.
- [ ] **Step 4: Run the selection tests + existing ssh selection tests — PASS.**
- [ ] **Step 5: Commit** — `feat(runs): require a git source for ephemeral_libvirt hosts`.

---

## Task 4: Extract `ShellBuildTransport` base (behavior-preserving SSH refactor)

**Files:**
- Create: `src/kdive/providers/build_host/shell_transport.py`
- Modify: `src/kdive/providers/build_host/ssh_transport.py`
- Test: `tests/providers/build_host/test_ssh_transport.py` (must stay green); add
  `tests/providers/build_host/test_shell_transport.py`

- [ ] **Step 1: Write a base-contract test** driving a tiny concrete subclass whose
  `_run_remote` is a fake recorder. Assert: `read_bytes` issues `base64 -w0 <path>` and
  b64-decodes; `read_text` utf-8-decodes and raises `CONFIGURATION_ERROR` on invalid utf-8;
  `read_bytes` over the size cap → `CONFIGURATION_ERROR`; `clone` issues
  `git init` / `git -C fetch --depth 1` / `git -C checkout FETCH_HEAD` with arg validation;
  `upload_file` builds the curl argv and parses the ETag; `cleanup` issues `rm -rf`.
- [ ] **Step 2: Run — FAIL** (`shell_transport` does not exist).
- [ ] **Step 3: Write `ShellBuildTransport`.** Move from `ssh_transport.py` the pure helpers
  (`_validate_git_arg`, `_validate_git_arg_url`, `_extract_etag_from_headers`,
  `_MAX_REMOTE_READ_B64_BYTES`, `_UNSAFE_CHARS`) and implement, in terms of an abstract
  `_run_remote(self, argv, *, cwd, timeout_s) -> CommandResult` and a `self._secret_registry`:
  `run`, `read_text`, `read_bytes`, `clone`, `upload_file`, `cleanup` (the exact bodies
  currently in `SshBuildTransport`, with `self._run_remote(...)` calls unchanged). `write_bytes`
  is declared abstract (raise `NotImplementedError` in the base — each subclass implements its
  framing). Keep `redacted_tail(..., self._secret_registry)` in error details.

```python
class ShellBuildTransport:
    """BuildTransport implemented over a single 'run one argv on the host' primitive.

    Subclasses provide ``_run_remote`` (ssh, guest-agent exec, ...) and ``write_bytes``
    (whose framing — stdin stream vs in-line pipeline — the primitive does not generalize).
    """

    _secret_registry: SecretRegistry

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        raise NotImplementedError

    def write_bytes(self, path: str, data: bytes) -> None:
        raise NotImplementedError
    # run / read_text / read_bytes / clone / upload_file / cleanup: moved verbatim from ssh.
```

- [ ] **Step 4: Repoint `SshBuildTransport`** to subclass `ShellBuildTransport`. Keep its
  `__init__` (set `self._address`/`self._identity_path`/`self._secret_registry`), `from_host`,
  `materialized_ssh_identity`, `_validate_ssh_destination`, `_ssh_argv`, and rename its private
  runner to `_run_remote(self, argv, *, cwd, timeout_s)` (the body of today's `_run_remote`,
  same `cd {cwd} && {join}` ssh wrap). Keep `write_bytes` (its stdin-streamed version). Delete
  the now-duplicated `run`/`read_text`/`read_bytes`/`clone`/`upload_file`/`cleanup` bodies and
  the moved helpers (import them from `shell_transport`).
- [ ] **Step 5: Run BOTH test files — PASS.** The existing SSH suite is the behavior-preserving
  guardrail. Run: `uv run python -m pytest tests/providers/build_host -q`.
- [ ] **Step 6: `just lint && just type`.**
- [ ] **Step 7: Commit** — `refactor(build): extract ShellBuildTransport base from ssh`.

---

## Task 5: `GuestExecBuildTransport` over the guest-agent exec channel

**Files:**
- Create: `src/kdive/providers/build_host/guest_exec_transport.py`
- Test: `tests/providers/build_host/test_guest_exec_transport.py`

- [ ] **Step 1: Write failing tests** with a fake `agent_command` (records the JSON
  guest-exec/-status round-trips, returns canned replies). Assert:
  - `run(["make","-C","/ws","x"], cwd="/ws", timeout_s=60)` issues a single guest-exec whose
    `path` is `/bin/sh` and `arg` is `["-c", "cd /ws && exec make -C /ws x"]`;
  - the per-call `timeout_s` is honored (a never-exiting fake → `TRANSPORT_FAILURE` after the
    monotonic deadline, using injected `monotonic`/`sleep`);
  - `read_bytes` issues `base64 -w0` and round-trips bytes; over the cap → `CONFIGURATION_ERROR`;
  - `write_bytes` issues `/bin/sh -c "printf %s '<b64>' | base64 -d > '<path>'"` and the fake
    captures the right base64;
  - `clone`/`upload_file`/`cleanup` reuse the base (inherited) and go through the agent;
  - `upload_file` registers the presigned URL in the secret registry before the exec (assert via
    a spy registry) and a curl failure puts the **query-stripped** URL in error details;
  - a non-zero in-guest exit maps as the base expects (e.g. `clone` checkout non-zero →
    `CONFIGURATION_ERROR`).
- [ ] **Step 2: Run — FAIL** (module does not exist).
- [ ] **Step 3: Implement.** Reuse `GuestAgentExec` unchanged.

```python
class GuestExecBuildTransport(ShellBuildTransport):
    """BuildTransport over the qemu-guest-agent exec channel (ADR-0100), one sh -c hop."""

    _SHELL = "/bin/sh"

    def __init__(self, *, domain: object, agent_command: AgentCommand,
                 secret_registry: SecretRegistry, poll_s: float = 1.0,
                 sleep: Sleep = time.sleep, monotonic: Monotonic = time.monotonic) -> None:
        self._domain = domain
        self._agent_command = agent_command
        self._secret_registry = secret_registry
        self._poll_s = poll_s
        self._sleep = sleep
        self._monotonic = monotonic

    def _agent_for(self, timeout_s: int) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({self._SHELL}),
            timeout_s=float(timeout_s), poll_s=self._poll_s,
            sleep=self._sleep, monotonic=self._monotonic,
        )

    def _exec_shell(self, command: str, timeout_s: int) -> CommandResult:
        result = self._agent_for(timeout_s).run(self._domain, [self._SHELL, "-c", command])
        return CommandResult(
            returncode=result.exit_status,
            stdout=result.stdout.decode("utf-8", "replace"),
            stderr=result.stderr.decode("utf-8", "replace"),
        )

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        return self._exec_shell(f"cd {shlex.quote(cwd)} && exec {shlex.join(argv)}", timeout_s)

    def write_bytes(self, path: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode("ascii")
        cmd = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        result = self._exec_shell(cmd, 60)
        if result.returncode != 0:
            raise CategorizedError(
                f"remote write_bytes failed for {path!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"path": path,
                         "stderr": redacted_tail(result.stderr, self._secret_registry)},
            )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        self._secret_registry.register(presigned.url)
        return super().upload_file(path, presigned)
```

  `AgentCommand`/`Sleep`/`Monotonic` import from `providers.remote_libvirt.guest.agent`.
  Override `upload_file`'s error detail to use the query-stripped URL — implement by catching
  the base's `INFRASTRUCTURE_FAILURE` and re-raising with `redact_presigned(presigned.url)`, OR
  add a `_upload_error_detail` hook on the base that ssh/guest-exec fill (prefer the hook to
  avoid catch-rewrap). Pick the hook approach: base `upload_file` calls
  `self._upload_url_detail(presigned.url)` (default: the raw url; guest-exec overrides to the
  stripped form). `redact_presigned` imports from `kdive.diagnostics.egress_probe`.
- [ ] **Step 4: Run the transport tests — PASS.**
- [ ] **Step 5: `just lint && just type`.**
- [ ] **Step 6: Commit** — `feat(build): add GuestExecBuildTransport over guest-agent exec`.

---

## Task 6: Build-domain XML + `EphemeralBuildVm` lifecycle

**Files:**
- Create: `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py`
- Modify: `src/kdive/providers/remote_libvirt/config.py` (build-VM vcpu/mem, if not present)
- Test: `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`

- [ ] **Step 1: Write failing tests** with a fake provision-connection (mirror
  `test_provisioning.py` fakes). Assert:
  - `render_build_domain_xml(run_id, pool, volume, network, machine, vcpus, mem_mib)` emits a
    `<channel>` virtio-serial `org.qemu.guest_agent.0`, the overlay `<disk>`, the network, the
    domain name `kdive-build-<run_id>`, and **no** `<qemu:commandline>` gdbstub args
    (regression: `recorded_gdb_port(xml) is None`);
  - `session(...)`: looks up the pool, ensures the overlay over `base_image_volume`, defines +
    starts `kdive-build-<run_id>`, waits for the agent, yields a `GuestExecBuildTransport`;
  - the `finally` tears down (destroy + undefine + delete overlay) — including when the yielded
    body raises (assert teardown ran);
  - idempotent provision: an already-defined/running domain is the achieved post-state.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement `render_build_domain_xml`** in
  `providers/remote_libvirt/lifecycle/xml.py` (or build_vm.py): reuse the existing domain-XML
  scaffolding (`KDIVE_METADATA_NS`, the overlay `<disk>`, the agent `<channel>`, the network),
  drop the gdbstub `<qemu:commandline>`, and use a build-specific name/uuid
  (`build_domain_name(run_id)` = `f"kdive-build-{run_id}"`; deterministic uuid from run_id).
  Define the disjoint overlay helpers (distinct from the System `overlay_volume_name` scheme):

```python
def build_domain_name(run_id: UUID) -> str:
    return f"kdive-build-{run_id}"

def build_overlay_volume_name(run_id: UUID) -> str:
    return f"kdive-build-{run_id}.qcow2"

def ensure_build_overlay(pool, base_image_volume: str, run_id: UUID):
    # mirror lifecycle.storage.ensure_overlay, but the overlay name is
    # build_overlay_volume_name(run_id) and the backing volume is base_image_volume.
    ...
```
- [ ] **Step 4: Implement `EphemeralBuildVm`** mirroring `RemoteLibvirtProvisioning`'s structure
  (config_factory, open_connection, secret_backend_factory, pki_base_dir, sleep/monotonic,
  agent timeout/poll injected). The `session` classmethod/contextmanager:

```python
@classmethod
@contextmanager
def session(cls, host, secret_registry, *, run_id, **seams) -> Iterator[GuestExecBuildTransport]:
    vm = cls(secret_registry=secret_registry, **seams)
    domain_name = build_domain_name(run_id)
    config = vm._config_factory()
    with vm._connection(config) as conn:
        pool = lookup_pool(conn, config.storage_pool)
        overlay = ensure_build_overlay(pool, host.base_image_volume, run_id)
        try:
            domain = vm._define_and_start(conn, run_id, config=config, overlay_name=overlay.name)
            wait_for_agent(conn, domain_name, monotonic=vm._monotonic, sleep=vm._sleep,
                           timeout_s=vm._agent_timeout_s, poll_s=vm._agent_poll_s)
            transport = GuestExecBuildTransport(
                domain=conn.lookupByName(domain_name),
                agent_command=qemu_agent_command,
                secret_registry=secret_registry,
            )
            yield transport
        finally:
            vm._teardown(conn, domain_name, config)   # destroy+undefine+delete overlay; best-effort
```

  `_define_and_start` mirrors provisioning's bounded define+start but with NO gdbstub port loop
  (a build VM has no gdbstub) — a single define+start, treating already-running as the achieved
  post-state. `_teardown` reuses `delete_volume` for the build overlay name. Add build-VM
  vcpu/mem to `RemoteLibvirtConfig` (env `KDIVE_REMOTE_LIBVIRT_BUILD_VCPUS` / `_BUILD_MEM_MIB`
  with sane defaults, e.g. 4 / 8192) only if no suitable sizing already exists; register the
  vars in `kdive.config` per ADR-0087 and regenerate the config reference (`just config-docs`).
- [ ] **Step 5: Run the build_vm tests — PASS.**
- [ ] **Step 6: `just lint && just type`** (the blocking libvirt bodies carry the established
  `# pragma: no cover - live_vm` where they cannot be unit-driven).
- [ ] **Step 7: Commit** — `feat(remote-libvirt): ephemeral build-VM provision/teardown`.

---

## Task 7: BUILD-handler `ephemeral_libvirt` branch

**Files:**
- Modify: `src/kdive/jobs/handlers/runs.py`
- Test: `tests/jobs/handlers/test_runs_build_handler.py` (extend)

- [ ] **Step 1: Write failing tests** with a fake builder + a fake `ephemeral_build_session`
  (a contextmanager yielding a fake transport, recording enter/exit). Assert:
  - an `ephemeral_libvirt` host runs the build over the yielded transport via
    `build_over_transport(...)` and returns the `BuildOutput`;
  - the session is entered before and exited after the build (teardown order), including on a
    build raise (teardown still runs — the `finally` in `session`);
  - a non-`RemoteLibvirtBuild` runtime builder selecting an `ephemeral_libvirt` host →
    `NOT_IMPLEMENTED`;
  - the lease is released on success only (reuse the existing lease-release assertions);
  - `git_remote`/`git_ref` come from the profile (reuse `_git_coords`).
- [ ] **Step 2: Run — FAIL** (`_run_build` has no ephemeral branch).
- [ ] **Step 3: Implement.** Add the patchable seam and the branch:

```python
# Patchable seam: tests substitute this to avoid a real libvirt provision.
ephemeral_build_session = EphemeralBuildVm.session

async def _run_build(conn, run, parsed, *, host, resolver, secret_registry) -> BuildOutput:
    run_id = run.id
    builder = (await _run_runtime(conn, run_id, resolver)).builder
    if host.kind == "local":
        return await asyncio.to_thread(builder.build, run_id, parsed)
    if host.kind == "ssh":
        with ssh_build_transport_from_host(host, secret_registry) as transport:
            bound = _build_over_ssh(builder, transport, host=host, parsed=parsed,
                                    run_id=run_id, secret_registry=secret_registry)
            return await asyncio.to_thread(bound.build, run_id, parsed)
    # ephemeral_libvirt
    with ephemeral_build_session(host, secret_registry, run_id=run_id) as transport:
        bound = _build_over_transport_host(builder, transport, host=host, parsed=parsed,
                                           run_id=run_id, secret_registry=secret_registry)
        return await asyncio.to_thread(bound.build, run_id, parsed)
```

  Rename `_build_over_ssh` → `_build_over_transport_host` (the body is host-kind agnostic — it
  already only requires `RemoteLibvirtBuild` + git coords) and call it from both the ssh and
  ephemeral branches; keep a thin `_build_over_ssh = _build_over_transport_host` alias if any
  test imports the old name, otherwise update call sites. The `NOT_IMPLEMENTED` guard message
  generalizes to "a remote build host is only supported for the remote-libvirt provider".
  Note `EphemeralBuildVm.session`'s blocking libvirt provision/teardown runs in the worker
  thread; the contextmanager body's `asyncio.to_thread(bound.build, ...)` offloads the build —
  consistent with the ssh branch (provision/teardown are short relative to the build).
- [ ] **Step 4: Run the handler tests — PASS.**
- [ ] **Step 5: `just lint && just type && uv run python -m pytest tests/jobs -q`.**
- [ ] **Step 6: Commit** — `feat(runs): build over an ephemeral libvirt VM`.

---

## Task 8: Reconciler — reap leaked build VMs (before lease reclaim)

**Files:**
- Create: `src/kdive/providers/remote_libvirt/build_vm_reaper.py`
- Modify: `src/kdive/reconciler/build_hosts.py`, the reconciler loop + composition that wires it
- Test: `tests/providers/remote_libvirt/test_build_vm_reaper.py`,
  `tests/reconciler/test_build_hosts.py` (extend)

- [ ] **Step 1: Write failing tests.**
  - Reaper seam: `RemoteLibvirtBuildVmReaper.list_build_vms()` returns one `BuildVm(domain_name,
    run_id)` per `kdive-build-<uuid>` domain (fake conn), ignores non-matching names;
    `delete_build_vm(name)` destroys+undefines+deletes the overlay; absent = no error.
  - `run_id_from_build_vm_name("kdive-build-<uuid>")` parses the UUID; a non-match → `None`.
  - Reconciler `reap_orphan_build_vms(conn, reaper)`: a `kdive-build-<run_id>` whose BUILD job is
    terminal/gone → reaped; one whose BUILD job is queued/running → NOT reaped; returns the
    count.
  - **Ordering (load-bearing):** the reconciler runs all repairs as an ordered `_RepairSpec`
    list in `reconciler/loop.py`; `reclaimed_build_host_leases` is at line 189 and the reaper
    specs (e.g. `reaped_dump_volumes`) run *after* it. Assert that the assembled repairs list
    places the new `reaped_build_vms` spec **immediately before** `reclaimed_build_host_leases`
    (find both by their string keys; assert the build-VM index < the lease-reclaim index), so a
    freed lease slot never coexists with a live leaked VM (spec §4.6).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement the reaper port** mirroring `RemoteLibvirtDumpVolumeReaper`
  (config_factory, open_connection, secret_backend_factory, pki_base_dir; `asyncio.to_thread`
  around the blocking `_list_blocking`/`_delete_blocking`). `_BUILD_VM_RE =
  re.compile(r"^kdive-build-(<uuid-re>)$")`; `_list_blocking` lists `kdive-build-*` domains;
  `_delete_blocking(name)` destroys (tolerate not-running), undefines (tolerate no-domain), and
  deletes `build_overlay_volume_name(run_id)` from the pool (tolerate already-gone).
- [ ] **Step 4: Implement the reconciler step.** In `reconciler/build_hosts.py`:

```python
async def build_vm_is_live(conn: AsyncConnection, run_id: UUID) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'build' AND (payload->>'run_id')::uuid = %s "
        "AND state IN ('queued', 'running') LIMIT 1",
        (run_id,),
    )
    return (await cur.fetchone()) is not None

async def reap_orphan_build_vms(conn: AsyncConnection, reaper: BuildVmReaper) -> int:
    reaped = 0
    for vm in await reaper.list_build_vms():
        if vm.run_id is not None and not await build_vm_is_live(conn, vm.run_id):
            await reaper.delete_build_vm(vm.domain_name)
            reaped += 1
    if reaped:
        _log.info("reconciler: reaped %d orphaned build VM(s)", reaped)
    return reaped
```

  Define a `BuildVmReaper` Protocol (`list_build_vms()`, `delete_build_vm(name)`) in
  `providers/reaping.py` (alongside `DumpVolumeReaper`) for the reconciler's injected seam, plus
  a `NullBuildVmReaper` (list → `[]`, delete → no-op) for deployments without remote-libvirt.
- [ ] **Step 5: Wire the reaper into the reconciler — concrete integration points.**
  `reconciler/loop.py` runs one ordered `_RepairSpec` list (lines 183-208). Add a
  `build_vm_reaper: BuildVmReaper = _NULL_BUILD_VM_REAPER` field to `ReconcileConfig` (mirroring
  the `dump_volume_reaper` field + `_NULL_DUMP_VOLUME_REAPER` singleton). In the repairs list,
  insert **immediately before** the `_RepairSpec("reclaimed_build_host_leases", …)` entry
  (currently line 189):

```python
        _RepairSpec(
            "reaped_build_vms",
            lambda conn: build_host_repairs.reap_orphan_build_vms(conn, config.build_vm_reaper),
        ),
        _RepairSpec("reclaimed_build_host_leases", _reclaim_build_host_leases),  # AFTER the reap
```

  In `providers/composition.py` add `build_reconciler_build_vm_reaper()` returning a
  `RemoteLibvirtBuildVmReaper` when `is_remote_libvirt_configured()` else `NullBuildVmReaper`
  (mirror `build_reconciler_dump_volume_reaper`), and pass it into the `ReconcileConfig` where
  `dump_volume_reaper` is set. Add `reaped_build_vms` to the reconcile counts struct/telemetry
  next to `reclaimed_build_host_leases`.
- [ ] **Step 6: Run the reaper + reconciler tests — PASS.**
- [ ] **Step 7: `just lint && just type && uv run python -m pytest tests/reconciler tests/providers/remote_libvirt -q`.**
- [ ] **Step 8: Commit** — `feat(reconciler): reap leaked ephemeral build VMs`.

---

## Task 9: Live exercise marker + full gate

**Files:**
- Modify: `tests/integration/live_stack/` or `tests/.../test_*_live.py` — add a `live_vm`-gated
  end-to-end build over an ephemeral VM (provision → in-guest make → publish → teardown),
  skipped in CI. Mirror the SSH live test if one exists; otherwise document the operator runbook
  step.
- Modify: the m2 portability/capture report only if a generated doc references build kinds.

- [ ] **Step 1:** Add the `@pytest.mark.live_vm` end-to-end test (or a runbook note if the live
  harness has no ephemeral image yet — state the limitation, do not un-gate or fake it).
- [ ] **Step 2: Run `just ci`** — all gates green (the `live_vm` suite stays skipped).
- [ ] **Step 3:** If `just docs` / `just config-docs` outputs changed (Tasks 2/6), confirm they
  are committed and `docs-check` / `config-docs-check` pass.
- [ ] **Step 4: Commit** any remaining generated-doc/test additions.

---

## Self-review (run after the plan, before execution)

- **Spec coverage:** §4.2 transport → Tasks 4–5; §4.3 lifecycle → Task 6; §4.4 handler → Task 7;
  §4.5 selection → Task 3; §4.6 reaper (reap-before-reclaim) → Task 8; §5 schema → Task 1; §6
  registration → Task 2; §7 redaction → Tasks 5 (URL register/redact) + 2 (no-secret audit); §8
  tests → each task's Step 1; live → Task 9. The shared-namespace invariant (no gdbstub port,
  disjoint overlay name) → Task 6 Step 3 + test.
- **No new taxonomy / no core-contract change:** confirmed — no `ErrorCategory`, `BuildOutput`,
  `Builder`-port, or `runs`-ledger edits in any task.
- **Type consistency:** `build_domain_name`/`build_overlay_volume_name`/`run_id_from_build_vm_name`
  are defined in Task 6 and reused by Task 8; `_build_over_transport_host` defined in Task 7
  supersedes `_build_over_ssh`; `ephemeral_build_session` seam defined in Task 7.
- **Ordering:** Task 1 (schema+dataclass) precedes Tasks 2/3/8 (need the column/field); Task 4
  (base) precedes Task 5 (subclass); Tasks 5+6 precede Task 7 (handler binds both); Task 8 needs
  Task 6's name/overlay helpers. No task depends on a later one.
