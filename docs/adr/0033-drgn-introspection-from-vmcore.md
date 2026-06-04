# ADR 0033 — Debug plane: drgn introspection from vmcore (offline) (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #22 (M0: Debug plane — drgn introspection from vmcore, offline)
- **Depends on:** [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the captured raw
  `vmcore` artifact this opens, the `debuginfo_ref`/recorded-`build_id` provenance
  resolution and the `fetch_object`/`read_vmcore_build_id` seams this reuses verbatim, and
  the realized-port + `live_vm`-gated-seam pattern this mirrors),
  [ADR-0029](0029-build-plane-local-make.md) (the Run's `debuginfo_ref` = `vmlinux` and the
  recorded `build_id` provenance), [ADR-0027](0027-safety-modules-secret-backend-impl.md)
  (the `Redactor` applied to the report), [ADR-0019](0019-tool-response-envelope.md) (the
  response envelope), [ADR-0001](0001-greenfield-rewrite.md) (the `ErrorCategory` taxonomy,
  including `debug_attach_failure`).
- **Refines:** the M0 Debug wording in
  [`../specs/m0-walking-skeleton.md`](../specs/m0-walking-skeleton.md) (the
  `introspect.from_vmcore` offline-introspection surface).
- **Spec:** [`../superpowers/specs/2026-06-04-drgn-introspection-from-vmcore-design.md`](../superpowers/specs/2026-06-04-drgn-introspection-from-vmcore-design.md)

## Context

ADR-0031 captured the kdump `vmcore` (raw `sensitive` + a redacted dmesg derivative) and
symbolized it against the Run's `debuginfo_ref` for **crash** postmortem. Issue #22 adds
the **offline drgn introspection** tier: open that same captured core with **drgn** on the
host (no live guest, no SSH), load the Run's `vmlinux` for symbols/types, and return a
minimal helper set — **tasks, modules, sysinfo** — as redacted structured data. The
`debug_attach_failure` `ErrorCategory`, the `artifacts`/`run_steps` tables, the captured
`vmcore` row, and the `Redactor` already exist; #22 adds the realized introspection port,
the tool, and one registrar append. **No schema migration is needed.** The decisions the
parent spec leaves open are settled here.

## Decision

### 1. `introspect.from_vmcore(run_id)` is a synchronous, ungated offline read — not a job

Like `postmortem.crash`/`.triage` (ADR-0031 §7), offline introspection is a read-only
inspection of an already-captured core: no destructive op, no admission gate, no job kind.
It takes a **`run_id`** (not `system_id`) because it needs the Run's `debuginfo_ref` (the
build-plane `vmlinux`) to load symbols/types; it resolves the Run's System and uses that
System's captured raw `vmcore`. It moves no durable-object lifecycle state and registers no
job handler. RBAC is project membership only.

### 2. M0 runs only the three fixed in-tree helpers — no caller-supplied drgn script

v1's `introspect.from_vmcore` takes an arbitrary user drgn `script` and renders it into a
sandboxed subprocess wrapper (base64 path encoding, byte caps, a `timeout`-bounded
`python3 -`). That is arbitrary code execution against kernel memory plus a whole
wrapper-rendering/parsing subsystem. The M0 acceptance asks only for "a minimal helper set
(tasks, modules, sysinfo)". So M0 runs **only** those three fixed, in-tree helpers — no
caller `script`, no wrapper rendering, no per-call byte cap, no subprocess `timeout`. The
arbitrary-script path and its wrapper/cap/timeout machinery return with the live
introspection tier in M1, where that execution surface is designed as a whole.

### 3. A realized `VmcoreIntrospector` port, seam-injected and `live_vm`-gated, mirroring `CrashPostmortem`

Opening the core with drgn, loading the `vmlinux`, and running each helper are **injected
seams** defaulting to real implementations guarded by `# pragma: no cover - live_vm`. So
the orchestration, the provenance check, the helper dispatch, and the redaction are
unit-tested with a fake drgn program; the real drgn path runs only under the existing
`live_vm` gate.

```python
class IntrospectOutput(NamedTuple):
    tasks: dict[str, object]; modules: dict[str, object]
    sysinfo: dict[str, object]; truncated: bool
class VmcoreIntrospector(Protocol):
    def from_vmcore(self, *, vmcore_ref: str, debuginfo_ref: str,
                    expected_build_id: str) -> IntrospectOutput: ...
```

The helpers use fixed in-tree caps (no caller args in M0: `tasks` blocked-only, `limit=200`)
and the assembled report is bounded by a total byte cap (`truncated` set on overflow,
`tasks` trimmed first), so the response can never be an unbounded multi-megabyte string.

`LocalLibvirtVmcoreIntrospect` realizes it, reusing the `fetch_object` and
`read_vmcore_build_id` seams `LocalLibvirtRetrieve` established, plus an `open_program`
seam (the drgn open) and a `run_helper` seam (one helper against the opened program).

### 4. drgn is confined to one typed seam; ty's `unresolved-import` is ignored only at that import line

drgn is a host package that may be ty-unresolvable in CI. Rather than scattering ignores,
the **entire** drgn dependency lives in the `open_program`/`run_helper` real seams, and the
helpers operate on a narrow typed `_Program` `Protocol` (the subset of methods they call).
The `import drgn` sits at one boundary with a single `# ty: ignore[unresolved-import]` (and
`# pragma: no cover - live_vm`). Every other module — the port orchestration, the tool, the
tests — is fully typed against the `Protocol`, so ty hard-gates the whole plane except the
one unavoidable import line.

### 5. Build-id provenance is verified before any helper runs (reused from postmortem)

drgn loading the wrong `vmlinux` against a core silently yields wrong symbols. So before
running any helper the port verifies the captured core's GNU build-id equals the build-id
the build plane recorded for the Run (`run_steps` `build` result `build_id`, ADR-0029 §5),
reusing `read_vmcore_build_id`. A mismatch is a `configuration_error` — the identical
provenance gate `LocalLibvirtRetrieve.run` enforces. The only whole-call
`debug_attach_failure` is drgn failing to **open** the core or **load** the vmlinux (the
genuine attach boundary). A helper raising mid-decode — including `modules`' all-failed
case (kernel-version/struct-offset skew, not an attach failure) — degrades to a per-helper
error marker / `all_failed` flag and the call still succeeds with a partial report;
escalating it would mislabel a decode-coverage gap as `debug_attach_failure`.

### 6. The report is redacted before it is returned and before any persistence

The helper output carries guest-derived strings (`comm`, module names, kernel-stack
frames, the boot cmdline, uts `version`). The assembled `{tasks, modules, sysinfo}` report
is run through the ADR-0027 `Redactor.redact_value` (structure-aware) **before it leaves
the port**, so it is redacted both when returned and before any later persistence. It is
returned in `ToolResponse.data["report"]` as a JSON string (`data` is `dict[str, str]`).

### 7. New module `mcp/tools/introspect.py`; one registrar append; no migration

The tool lives in its **own** module `mcp/tools/introspect.py` (not `mcp/tools/debug.py`,
which a sibling issue owns), registered by appending `introspect.register` to
`_PLANE_REGISTRARS` in `mcp/app.py`. No `_HANDLER_REGISTRARS` change (no job kind). No
schema migration.

## Consequences

- Offline introspection is a synchronous, ungated, lifecycle-neutral read keyed on
  `run_id`, mirroring `postmortem.crash`; the captured core and the `vmlinux` provenance
  are reused, not re-derived.
- The full introspection logic is unit-testable with a fake drgn program + fake store; the
  real drgn path is `live_vm`-gated, so CI stays green with no drgn install or host — and
  unlike the live/gdbstub planes, this plane has **real, non-gated** tests against the
  fake.
- drgn — possibly ty-unresolvable — is confined to one seam and one ignored import line;
  the rest of the plane is fully ty-gated against a typed `Protocol`.
- A wrong-vmlinux core is a `configuration_error` (provenance); a drgn open/decode failure
  is a `debug_attach_failure`; an unbuilt Run or core-less System is a `configuration_error`.
- All guest-derived output is `Redactor`-scrubbed before return and before any persistence.
- `mcp/app.py` gains one tuple append + one import; `mcp/tools/introspect.py` and
  `providers/local_libvirt/introspect_drgn.py` are new. **No schema migration.**

## Considered & rejected

- **Implement `introspect.run` (live drgn-over-SSH) now.** Rejected: it needs a guest SSH
  transport + credentials (secret backend) the M0 walking-skeleton path does not otherwise
  require, and `introspect.run` is not in the M0 tool subset — it is scope creep. The issue
  explicitly defers it to M1.
- **Accept a caller-supplied drgn `script` (the v1 shape) in M0.** Rejected: arbitrary code
  execution against kernel memory plus a wrapper-rendering/byte-cap/`timeout`-subprocess
  subsystem is a large surface the M0 acceptance does not ask for (it asks only for the
  three named helpers). The script path returns with the live tier in M1 where its
  execution model is designed whole. M0 runs only the three fixed helpers.
- **Put `introspect.from_vmcore` in `mcp/tools/debug.py`.** Rejected: a sibling issue (#20)
  concurrently creates `debug.py` on a different base; #22 depends on #24, not #20, so
  `debug.py` does not exist on this base. Co-locating would force a cross-issue ordering and
  a merge conflict on a file #22 does not own. The tool gets its own module
  `mcp/tools/introspect.py`; the planes stay independent.
- **Run the helpers in a subprocess (`python3 -`) like v1.** Rejected: v1's subprocess +
  wrapper exists to sandbox a *caller-supplied* script and to bound it with a `timeout`.
  With only three fixed in-tree helpers there is no untrusted code to sandbox, so M0 runs
  them in-process against the opened `Program` behind the `live_vm` seam — no wrapper
  rendering, no framed-JSON parsing, no per-call subprocess. The subprocess sandbox returns
  with the caller-script path in M1.
- **Skip the build-id provenance check (just open whatever `vmlinux` the Run names).**
  Rejected: a `vmlinux` whose build-id does not match the core makes drgn emit silently
  wrong symbols/types — a worse failure than an error. The provenance gate (reused from
  `postmortem.crash`) turns it into a clean `configuration_error`.
- **`live_vm`-gate the whole plane with no real tests (like the gdbstub/libvirt planes).**
  Rejected: the acceptance explicitly says this path "can have real, non-gated tests against
  a fixture/mocked drgn program". Only the drgn *open/helper* seams are gated; the
  orchestration, provenance, dispatch, and redaction run for real in CI against a fake
  program — that is the bulk of the logic and the bulk of the risk.
- **Persist the redacted introspection report as an `artifacts` row now.** Rejected: the
  acceptance asks that the tool *returns* the data redacted, not that it persists a row.
  Redaction already happens before the value leaves the port, so a later persistence is of
  already-redacted text — adding the row is row-insertion only and is deferred to keep #22
  migration-free and focused.
- **Port the v1 `check_prerequisites` / `drgn_probe` path too.** Rejected: that path probes
  a **live target over SSH** for drgn/debuginfo readiness; the offline-vmcore M0 path has no
  live target to probe. Its one reusable piece — build-id normalization — is subsumed by the
  provenance check the port already does via `read_vmcore_build_id`.
- **Scatter `# ty: ignore` across the drgn call sites.** Rejected: that leaks the
  unresolvable dependency through the plane. Confining drgn to one seam behind a typed
  `_Program` `Protocol` keeps ty hard-gating everything except the single unavoidable
  `import drgn` line.
