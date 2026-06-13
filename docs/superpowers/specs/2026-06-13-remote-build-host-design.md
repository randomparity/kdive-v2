# Remote build-host targets — design

- **Date:** 2026-06-13
- **Issue:** [#342](https://github.com/randomparity/kdive/issues/342) (`status:needs-design`)
- **ADR:** [ADR-0099](../../adr/0099-remote-build-host-targets.md)
- **Status:** Approved (design); implementation scoped to the SSH target (see §9)

## 1. Problem

The original build plane had two delivery paths: (1) **upload artifacts built locally**
and (2) **request a build from a remote build server**. Path 1 ships today as the
external-build lane (`source="external"`). Path 2 does **not** exist: the server-build
lane (`source="server"`) runs `make` **in the worker process itself**
(`providers/build_host/execution.py:real_run_make` → `subprocess.run(["make", …])`,
offloaded via `asyncio.to_thread`). There is no port that ships a build to a machine that
is not the worker and pulls the artifacts back.

Consequences of the worker-only model:

- The worker image must carry the full kernel toolchain **and** a warm kernel source tree
  (`KDIVE_KERNEL_SRC`). On a demo app-pod worker neither is present, so the server lane
  cannot run there at all.
- Builds compete with the worker for CPU/memory/disk, bounded by the worker pod's
  resources and the in-process 2h `make` timeout.
- There is no way to point builds at a dedicated builder (or several) distinct from the
  control-plane worker.

## 2. Goal

Let a server-lane Run dispatch its checkout + `make` to a machine that is not the worker,
then ingest the resulting `kernel_ref` / `debuginfo_ref` / `build_id` back into the `runs`
ledger **exactly** as the in-worker path does — preserving the kdump/debuginfo config
preflight and the build error taxonomy. The in-worker path remains the default; remote is
opt-in.

This design covers **three build targets** as one coherent model. Implementation in the
first PR is scoped to target 2 (§9); targets 1 and 3 are described so the seam and schema
admit them without rework.

- **Target 1 — local upload.** The existing `source="external"` lane. Unchanged;
  documented here only to place it in the model.
- **Target 2 — dedicated SSH build host.** An admin-registered host reachable over SSH.
  **Implemented in this PR.**
- **Target 3 — ephemeral remote-libvirt build VM.** A build VM provisioned on demand on a
  remote-libvirt host, the build dispatched over the in-guest exec/presigned channel
  (#202), the VM torn down afterward. **Follow-up issue.**

## 3. Non-goals

- Replacing the worker-local build path. It stays as the default and as `kind='local'`.
- A build-agent process that polls the job queue (a new deployment surface; rejected — see
  ADR-0099).
- Cross-host build caching / ccache sharing, build-host autoscaling, or multi-arch build
  matrices. Out of scope.
- Queuing builds when a host is at capacity. The contract is fail-fast (§6); a FIFO queue
  is a possible later extension (ADR-0099 alternatives).

## 4. Architecture

### 4.1 The seam stays where it is

`BuildHostOrchestrator.build_workspace` (`providers/build_host/orchestration.py`) already
abstracts every slow step behind injected callables:

```
build_workspace(run_id, profile):
    resolve config fragment           (worker-side; catalog/local ref)
    checkout(run_id, profile, ws, frag)     ← seam
    run_olddefconfig(ws)                     ← seam
    read_config(ws) -> str                   ← seam
    _validate_final_config(...)        (worker-side; fragment-survival + kdump/debuginfo)
    run_make(ws)                             ← seam
```

A remote build host is therefore **not** a new orchestrator. It is a new realization of
those seams that runs each step over a transport. The config fragment resolution and
`_validate_final_config` stay on the worker: the worker resolves the fragment, the remote
host merges/builds, the worker reads the resulting `.config` back over the transport and
validates it. `BuildOutput`, the `Builder` port, and the `runs` ledger are untouched.

### 4.2 The `BuildTransport` port

Introduce one new port abstracting the host-side primitives the seams need:

```python
class BuildTransport(Protocol):
    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult: ...
    def read_text(self, path: str) -> str: ...
    def read_bytes(self, path: str) -> bytes: ...
    def clone(self, remote: str, ref: str, dest: str) -> None: ...
    def cleanup(self, path: str) -> None: ...
    def presign_put(self, name: str) -> PresignedUpload: ...   # worker mints, host uploads
```

Two realizations:

- **`LocalBuildTransport`** — wraps today's behavior: `run` → `subprocess.run` with fixed
  argv (no shell), `read_text`/`read_bytes` → `Path.read_text/read_bytes`, `clone` is a
  no-op for the warm-tree model, `presign_put` is unused (worker PUTs directly). This is a
  **pure refactor**: the local path's observable behavior does not change.
- **`SshBuildTransport`** — `run`/`read_*`/`clone` execute over `ssh`/`sftp` with fixed
  argv and an identity file materialized from `ssh_credential_ref`; `presign_put` returns a
  worker-minted presigned S3 PUT URL the host uploads to.

The seams (`Checkout`, `RunStep`, `ReadConfig`, and the modules-install / bundle / read
helpers) gain a transport-backed implementation that delegates to the port. The orchestrator
contract does not change.

### 4.3 Builder selection

The builder a Run uses is chosen by **build-host selection** (§5), not hard-wired to the
provider. Selection resolves a `build_hosts` row; `kind='local'` → `LocalBuildTransport`
(== today), `kind='ssh'` → `SshBuildTransport`. The provider runtime continues to expose a
`builder`; selection picks the transport the builder runs on.

Selection and **capacity admission happen synchronously at the `runs.build` tool boundary**,
before the BUILD job is enqueued — so `capacity_exhausted` is a real synchronous tool error
the caller can act on immediately, not a failed Run discovered later by polling (the build is
an async job per ADR-0018; a failure raised inside the handler would surface only as a FAILED
Run, and a Run is single-shot through build). The handler then runs the build against the
already-selected, already-admitted host.

```
runs.build (tool)
    resolve build host (profile.build_host | default 'worker-local')
    provenance/kind compatibility check (§7)  → configuration_error on mismatch
    if kind != 'local': acquire a build-host lease (§6)  → capacity_exhausted if full
    enqueue BUILD job (carries the resolved build_host id)
        ── BUILD job ──→  build_handler
            builder.build(run_id, profile)  on the selected transport
                BuildHostOrchestrator.build_workspace()   (unchanged contract)
            finalize_build(...)   (unchanged ledger)
            release the build-host lease (idempotent, on success AND failure)
```

The resolved `build_host` id is recorded on the Run (or its BUILD job payload) so the handler
and the reconciler agree on which host the build holds a lease against.

## 5. `build_hosts` inventory

New table (migration 0027, additive / forward-only per ADR-0015):

| column | type | notes |
|--------|------|-------|
| `id` | uuid PK | |
| `name` | text UNIQUE NOT NULL | operator-facing selector |
| `kind` | text NOT NULL | CHECK in (`'local'`, `'ssh'`); `'ephemeral_libvirt'` admitted later |
| `address` | text NULL | `user@host:port` for `ssh`; NULL for `local` |
| `ssh_credential_ref` | text NULL | secret-by-reference (private key); never the key bytes |
| `workspace_root` | text NOT NULL | per-run checkout scratch dir on the host |
| `max_concurrent` | integer NOT NULL CHECK > 0 | capacity ceiling (gated kinds only) |
| `enabled` | boolean NOT NULL DEFAULT true | operator on/off toggle (drain) |
| `state` | text NOT NULL DEFAULT `'ready'` | CHECK in (`'ready'`, `'unreachable'`); **reconciler-owned** health |
| `updated_at` | timestamptz NOT NULL DEFAULT now() | `set_updated_at` trigger |

In-flight builds are **not** counted with an integer column. This codebase models capacity
by counting non-terminal rows (`services/systems/admission.py`: "System states that occupy a
per-project quota slot"), and a bare counter cannot be credited idempotently or reclaimed by
attribution. Instead a companion table records one row per in-flight remote build:

| `build_host_leases` column | type | notes |
|--------|------|-------|
| `run_id` | uuid PK | one lease per Run; makes acquire/release idempotent |
| `build_host_id` | uuid NOT NULL REFERENCES build_hosts(id) | the host this build occupies |
| `acquired_at` | timestamptz NOT NULL DEFAULT now() | observability only (not a reclaim trigger) |

**Capacity = `COUNT(*) FROM build_host_leases WHERE build_host_id = ?`**, evaluated under the
per-host lock (§6). Acquire = `INSERT` (PK on `run_id` makes a retry a no-op); release =
`DELETE WHERE run_id = ?` (idempotent). Reclaim is by **owning-job liveness**, not age (§8).

A **seed row** preserves today's behavior:

```sql
INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent)
VALUES (<fixed-uuid>, 'worker-local', 'local', '/var/lib/kdive/build', <high-sentinel>);
```

`kind='local'` builds are **not capacity-gated**: a worker claims one job at a time
(`jobs/worker.py` sequential claim loop), so local build concurrency is already bounded by the
worker fleet, and a single shared `worker-local` row cannot meaningfully model per-replica
local capacity. Local builds therefore acquire **no lease** — preserving today's behavior
exactly (no new throttle on concurrent local builds across worker replicas). The seed's
`max_concurrent` is informational for the local row; `workspace_root` is informational too
(the local transport reads `KDIVE_BUILD_WORKSPACE` as it does today). With no other host
registered, every server-lane build resolves to `worker-local` → `LocalBuildTransport` → the
current behavior.

**Registration plane** (`mcp/tools/ops/build_hosts/`): `build_hosts.register`,
`build_hosts.list`, `build_hosts.disable`, `build_hosts.remove`.

- `register` / `disable` / `remove` are `PLATFORM_ADMIN`, `(principal, ...)` audited
  (creating/removing remote-exec infrastructure). `register` validates the `ssh_credential_ref`
  shape (present, resolvable by reference) but does not persist key bytes.
- `list` is read-only-by-policy passthrough (operator visibility).
- `disable` is the operator drain toggle: it sets `enabled=false`, which stops new lease
  acquisitions (selection rejects it) while existing leases finish. `enabled` is the only
  operator-writable availability field; `state` is reconciler-owned health (`ready` ↔
  `unreachable`) and is never written by an admin tool.
- `remove` refuses a host that still has a `build_host_leases` row (returns `conflict`, also
  FK-enforced via `ON DELETE RESTRICT`); drain via `disable` first, let leases drain, then
  remove.
- The built-in `worker-local` seed row is protected: `disable` and `remove` refuse it
  (`conflict`), because it is the default fallback every no-name build resolves to —
  disabling or removing it would break all default-target builds.

**Selection** at the `runs.build` tool boundary (§4.3):

1. `profile.build_host` names a host → resolve it. Absent row → `not_found`. `enabled=false`
   or `state='unreachable'` → `configuration_error` (named but unusable).
2. No name → default to `worker-local`.

A `kind`/provenance compatibility check fails closed (§7): a `local` host with a git
`kernel_source_ref`, or an `ssh` host with a warm-tree string, is `configuration_error`.

## 6. Capacity (fail-fast)

Capacity uses the existing transaction-scoped advisory-lock + check-then-act pattern (a new
`LockScope.BUILD_HOST` keyed by host id). `advisory_xact_lock` releases at commit, so it
guards only the short admission transaction — it is **never** held across the build:

```
# At the runs.build tool boundary, for kind != 'local'. The lease INSERT and the BUILD-job
# enqueue share ONE transaction, so there is no window where a committed lease has no job
# (which the reconciler would otherwise see as "job gone" and reclaim, or which would orphan):
with conn.transaction(), advisory_xact_lock(conn, LockScope.BUILD_HOST, host.id):
    if (SELECT count(*) FROM build_host_leases WHERE build_host_id = host.id) >= max_concurrent:
        raise CategorizedError(category=CAPACITY_EXHAUSTED, ...)   # fail fast, synchronous
    INSERT INTO build_host_leases (run_id, build_host_id) VALUES (<run_id>, host.id)
    # PK on run_id → a retried tool call for the same Run is a no-op, not a double-acquire
    enqueue BUILD job (dedup_key on run_id)        # same transaction; commits atomically

# In the build handler, after the build reaches a terminal outcome:
DELETE FROM build_host_leases WHERE run_id = <run_id>   # idempotent, on success AND failure
```

The lease is the durable, attributed record a bare counter lacked: acquire/release are
idempotent on `run_id`, and the reconciler reclaims a lease by checking the owning BUILD
job's liveness — not a timer (§8), so a legitimate multi-hour build keeps its slot for the
full `MAKE_TIMEOUT_S`. Over-capacity returns a typed `capacity_exhausted` synchronously from
`runs.build` (before any job is enqueued), with `suggested_next_actions` pointing the caller
at a retry.

**New taxonomy value.** `ErrorCategory.CAPACITY_EXHAUSTED = "capacity_exhausted"`. The
closest existing values are wrong: `quota_exceeded` is per-project accounting, not host
capacity; `infrastructure_failure` is non-specific. Adding a value follows the ADR-0097
precedent (added `not_found`/`conflict` for a genuine new failure mode) and requires its
exit-code mapping, taxonomy docs, and the closed-set test to be updated in the same change.

## 7. Source provenance, config, secrets

### 7.1 Git-clone provenance (ssh builder)

Today `ServerBuildProfile.kernel_source_ref` exists but the **real checkout ignores it** and
rsyncs the worker's warm `KDIVE_KERNEL_SRC`. To make the field unambiguous across builders,
the server lane accepts either form:

- **plain string** → warm-tree provenance (local builder; unchanged).
- **object `{git: {remote, ref}}`** → git-clone provenance (ssh builder).

The ssh checkout must resolve an **arbitrary** ref or commit sha, so it does **not** use
`git clone --depth 1` (which fetches only the default-branch tip, making a subsequent
`git checkout <other-ref>` fail with "reference is not a tree"). It uses an init+fetch model:
`git init <ws>` → `git -C <ws> fetch --depth 1 <remote> <ref>` → `git -C <ws> checkout
FETCH_HEAD`. This resolves a branch, tag, or full sha in one shallow fetch (a server-side
allowance most hosts grant; the spec notes the fallback to a non-shallow `fetch` if the
remote refuses by-sha shallow fetch). This corrects ADR-0099 decision 5's "clone --depth 1"
wording.

Fail-closed cross-checks at the `runs.build` boundary (§4.3): the **local** builder rejects a
git ref; the **ssh** builder rejects a warm-tree string. No silent mismatch. **Coupling
note:** because the cross-check binds a profile's `kernel_source_ref` shape to a builder kind,
a profile is authored *for* a builder kind — a warm-tree profile cannot be retargeted to an
ssh host without rewriting `kernel_source_ref` to the git form (and vice versa). This is an
intentional constraint, surfaced to the operator as a `configuration_error`, not an
orthogonal axis. The git remote/ref are validated for shape (no shell metacharacters; argv is
fixed); a private remote uses a secret-ref credential resolved at the worker boundary like the
SSH key.

### 7.2 Config preflight unchanged

The worker resolves the config fragment (catalog/local ref, ADR-0096) and ships the fragment
bytes to the host. When the profile names a `patch_ref`, the worker also resolves it (against
the component-root allowlist) and ships the **patch bytes** to the host the same way — the
existing checkout applies the patch *inside the workspace* (`workspace.py:apply_patch` runs
`git apply` there), so for the ssh builder both the apply and its silently-skipped guards
(which snapshot workspace files before/after) execute remotely over the transport; the worker
reads back the guard inputs it needs to make the same skip/no-op determination. `merge_config.sh`
+ `make defconfig` + `make olddefconfig` run on the host via the transport. The worker reads
the resulting `.config` back (transport `read_text`) and runs the **same**
`_validate_final_config` (fragment-survival + kdump/debuginfo OR-groups + profile
requirements). The component-root allowlist gates `config`/`patch_ref` on the worker side
before anything ships.

### 7.3 Secrets and redaction

`ssh_credential_ref` (and any git credential) resolve at the worker boundary, register into
the redaction registry for the op's lifetime, and only `(present, source-ref)` persists —
the #077 x509-cert contract. The key is materialized to a private temp identity file
(mirroring `materialized_pkipath`), `ssh`/`sftp` run with fixed argv + `-i`, the file is
removed in `finally`. All remote stdout/stderr passes the redactor (`redacted_tail`) before
any response snippet or persistence.

### 7.4 Security posture of build execution

`build_hosts.register` is the gated admin op. Running a build on a registered host is a
normal server-lane build and is **not** added to the destructive-op gate
(`security/gate.py`): it is not power/teardown/force_crash, consistent with how local and
remote builds work today. Bounding controls: the host is admin-registered (not
caller-supplied), the clone lands in an isolated per-run subdir of `workspace_root`, the
component-root allowlist gates config/patch, fixed argv prevents shell injection, and `make`
runs under the existing 2h timeout. ADR-0099 records this with rationale.

## 8. Reconciler

The reconciler gains build-host upkeep (drift repair, ADR-0021 family):

- **Lease reclaim by owning-job liveness, never by age.** A `build_host_leases` row whose
  owning Run/BUILD job is terminal or gone (failed, cancelled, no job row) is deleted, freeing
  the slot. The trigger is the owning job's state — **not** elapsed time: a legitimate build
  can run up to `MAKE_TIMEOUT_S` (2h), so an age threshold would reclaim a live build's slot
  and over-admit the host past `max_concurrent`. The reconciler joins `build_host_leases` to
  the BUILD job (via `run_id`) and reclaims only when that job is not live. Delete is idempotent.
- **Health.** A periodic reachability probe (cheap `ssh true` over the transport) flips a
  host `state` `ready` ↔ `unreachable`; `unreachable` blocks new lease acquisition (§5) but
  does not delete the row or existing leases.

## 9. Implementation scope (this PR)

In:

- Migration 0027: `build_hosts` + `build_host_leases` tables + `worker-local` seed.
- `ErrorCategory.CAPACITY_EXHAUSTED` + exit-code mapping + taxonomy docs + closed-set test.
- `BuildTransport` port; `LocalBuildTransport` (refactor of today's behavior, no change);
  `SshBuildTransport`.
- Transport-backed seam realizations wired through `BuildHostOrchestrator` (contract
  unchanged), including shipping config-fragment and patch bytes to the host.
- `ServerBuildProfile.build_host` (optional) + structured `kernel_source_ref` (string |
  `{git}`), parse-boundary validation + fail-closed provenance cross-checks.
- `build_hosts.*` admin plane (register/list/disable/remove) with RBAC + audit + secret-ref
  validation.
- `runs.build` tool-boundary selection + fail-fast lease acquisition (local skips the gate);
  handler-side idempotent lease release on success and failure; the resolved host id recorded
  on the Run/BUILD payload.
- Reconciler build-host lease reclaim (job-liveness) + reachability health.
- ADR-0099, this spec, and the implementation plan.

Out (follow-up issues):

- Target 3 (`kind='ephemeral_libvirt'`): build-VM provisioning lifecycle, in-guest exec
  dispatch, reconciler reaping of the ephemeral builder.
- FIFO build queue on capacity.
- Build caching / ccache.

## 10. Testing

Boundary: drive the builder/transport directly with injected fakes (repo convention — no
real SSH in unit tests). The real `ssh`/`make` path is exercised only under the `live_vm`
gate (unchanged gate; not widened).

- **Transport seam:** a fake `BuildTransport` records argv and returns canned files; assert
  the ssh seams produce the same orchestrator calls as local, and that `_validate_final_config`
  runs on the worker against the read-back `.config`.
- **Provenance fail-closed:** local+git → `configuration_error`; ssh+warm-string →
  `configuration_error`; ssh+git → clone argv well-formed and shape-validated.
- **Selection:** named-but-absent → `not_found`; named-but-disabled → `configuration_error`;
  no name → `worker-local`.
- **Capacity:** at ceiling → `capacity_exhausted` synchronously from `runs.build`; concurrent
  admissions under the advisory lock never exceed `max_concurrent` (adversarial/hypothesis
  race test on lease-row count); lease released on both success and failure; a retried
  tool call / build job for the same Run does not double-acquire (PK on `run_id`); a
  `kind='local'` build acquires no lease (no throttle on concurrent local builds).
- **Registration RBAC/audit:** non-admin → `authorization_denied`; register audits
  `(principal, ...)`; secret never persisted/leaked (no-leak guard on the row + error
  details); `remove` with an outstanding lease → `conflict`; `disable` sets `enabled=false`
  and blocks new acquisition; an admin tool cannot write `state`; `disable`/`remove` of the
  built-in `worker-local` row → `conflict` (protected default fallback).
- **Redaction:** remote stderr containing the key/credential is redacted in error details.
- **Reconciler:** a lease whose BUILD job is terminal/gone is reclaimed; a lease whose BUILD
  job is still live is **not** reclaimed (no age-based reclaim); unreachable host blocks
  acquisition.
- **Migration:** schema test for `build_hosts` + `build_host_leases` + seed; `worker-local`
  present after migrate.
- **Back-compat:** with only the seed row, `LocalBuildTransport` issues the identical
  subprocess argv / orchestrator call sequence as the pre-refactor path (assert the call/argv
  sequence — not a golden `BuildOutput`, whose object-store keys are minted per run and are
  not stable across runs).
