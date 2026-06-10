# Spec: operator-run remote live-stack e2e + milestone-end portability report (#207)

**Status:** draft
**Issue:** [#207](../../../issues/207) — M2 issue 8 (the milestone capstone).
**Settled by:** [ADR-0042](../../adr/0042-live-stack-e2e-mcp-http.md) (the operator-run
live-stack e2e shape this mirrors), [ADR-0076](../../adr/0076-remote-libvirt-provider-package.md)
(the portability gate + the milestone-end report), and the remote-spine mechanics ADRs
[0079](../../adr/0079-remote-live-debug-transport.md) /
[0080](../../adr/0080-remote-provisioning-disk-image-profile.md) /
[0082](../../adr/0082-remote-install-in-guest-kernel.md) /
[0083](../../adr/0083-remote-connect-debug-plane.md) /
[0084](../../adr/0084-remote-control-two-phase-vmcore-retrieve.md). **No new ADR.** Issue 8
introduces no architectural decision with viable alternatives — it is a proving run plus a
recorded measurement over decisions already made. The design altitude here is test placement
and report generation, captured below.

## Why this issue exists

M2 has two co-equal goals ([spec](../../specs/m2-remote-libvirt.md)): a working remote
capability, and a *checkable* portability hypothesis. Issue 8 delivers the two capstone
artifacts that demonstrate both against a real second provider for the first time:

1. **An operator-run live-stack e2e** that drives the full spine — allocate → provision →
   build → install → boot → attach → force-crash → capture vmcore → release — against a
   genuinely remote `qemu+tls://` host, over the live MCP HTTP transport, under per-project
   role tokens. It mirrors M1.2's `test_spine_over_the_wire` (ADR-0042); operator-run, **not**
   CI.
2. **The milestone-end portability report**: the CI diff gate (`scripts/m2_portability_gate.py`,
   ADR-0076) ships in issue 1 and runs per-PR; issue 8 *records* the milestone-end measurement
   (cumulative touched lines vs the `pre-M2` tag) as a committed report.

## Non-goals

- **No new MCP tools, no new `ErrorCategory`, no core changes.** The remote spine drives the
  *existing* surface (`resources.*` / `allocations.*` / `systems.*` / `runs.*` / `debug.*` /
  `control.*` / `vmcore.*`), exactly as the local spine does, selecting the remote resource by
  kind. This PR touches only `tests/`, `docs/`, `scripts/`, and `justfile` — **zero** files
  under the gate's core prefixes — so it cannot trip the very gate it reports on.
- **No un-gating.** The new e2e is `live_stack`-marked and preflights to a clean skip; CI
  deselects `live_stack`. It is never run in CI here.
- **No in-guest drgn-live MCP routing.** That is the deferred ADR-0083 follow-up (#215) — it
  generalizes `start_session` / `introspect.run`, the live in-guest path, **not** `from_vmcore`.
  The remote spine's introspection phase asserts the **worker-side vmcore postmortem**
  (`introspect.from_vmcore`), which already resolves the per-run runtime via
  `with_runtime_for_run` (`mcp/tools/debug/introspect.py`) and so routes to the remote runtime's
  `debug_common` postmortem the moment a remote core exists. The phase asserts a **non-empty,
  redacted** report keyed to the remote `run_id` (no secret leak) — the same falsifiable signal
  the local spine asserts.

## Deliverable 1 — the remote spine e2e

### Placement and shared scaffolding

`tests/integration/test_live_stack.py` carries reusable spine scaffolding inline: the
`phase` / `SpinePhaseError` naming contract, `_drain_job`, `_await_system_state`, the
`_ok` / `_scalar` envelope helpers, the per-role `_token` factory, and the out-of-band DB
helpers (`_grant_force_crash_scope`, `_seed_metering`, audit/teardown asserts). A second spine
needs the same scaffolding.

**Decision:** extract the provider-agnostic scaffolding into
`tests/integration/live_stack/spine.py` and have **both** the local and the remote spine import
it. One copy, two drivers — the "replace, don't deprecate / no duplication" standard. The local
test keeps its `local-libvirt`-specific profile factories and its full assertion body; only the
shared helpers move.

- *Considered & rejected:* importing helpers directly from the `test_live_stack` pytest module.
  Rejected — importing a collected test module for its helpers is fragile (collection side
  effects; the module also defines non-gated unit tests), and couples the remote spine to the
  local spine's file rather than to a named shared seam.
- *Considered & rejected:* a fresh private copy of the helpers in the remote test. Rejected —
  two copies of the phase/drain contract drift; a fix to one silently misses the other.

The remote spine lives in `tests/integration/test_remote_live_stack.py`.

### What the remote spine differs on (vs the local spine)

| Phase | Local spine | Remote spine |
|-------|-------------|--------------|
| allocate | `resource: {"mode": "kind"}` (defaults local-libvirt) | `resource: {"mode": "kind", "kind": "remote-libvirt"}` |
| provision | `direct-kernel`, local rootfs path | `disk-image` profile (ADR-0080): `base_image_volume` from env, `crashkernel`, `destructive_ops: ["force_crash"]` |
| attach | gdb-MI over local loopback gdbstub | gdb-MI over **direct TCP** to the host gdbstub port (worker-pool-ACL'd; host/port resolved server-side from operator config + domain XML) |
| crash | `control.force_crash` (injectNMI) | same tool; remote `RemoteLibvirtControl` injectNMI → panic → kdump (ADR-0084) |
| capture | `vmcore.fetch` → drain | `vmcore.fetch(method="kdump")` → drain; **two-phase** KDUMP-only (ADR-0084; `fetch` defaults to `host_dump`, so remote pins `kdump`). Assert a **redacted** vmcore artifact, no raw core leaked. |
| introspect | `introspect.from_vmcore` | same tool, routed to the remote runtime; assert a non-empty redacted report keyed to the remote run |

**The crashed-System state contract is unchanged for remote.** `vmcore.fetch` admits a
`capture_vmcore` job only on a `crashed` System (`mcp/tools/lifecycle/vmcore.py`), and the
System *row* stays `crashed` across the two-phase capture — the guest's kdump→reboot→agent-upload
cycle is **internal to the remote `capture()` job**, not a row transition the spine observes. So
the spine reuses the shared `_await_system_state(..., "crashed")` → `vmcore.fetch` → `_drain_job`
sequence verbatim. The one remote-specific budget: the capture job waits out a server-side
readiness window (`retrieve.py` `_readiness_timeout_s`, default **300s**) while the guest reboots
out of the capture kernel, then uploads. The spine drains that job, so the drain deadline must
cover readiness + reboot + upload. The shared `_DRAIN_DEADLINE_S` (600s) is the budget; the
remote spine confirms it brackets the 300s readiness default, and the runbook flags raising it
if the operator's reboot is slow.

Everything else (open-investigation, create-run, build, install, boot, release, reconciler
teardown, **and the accounting report phase**) is identical and uses the shared helpers. The
report phase is **kept** for the remote run: it is provider-agnostic (accounting over the
ledger) and writes `accounting-report.json` — the durable, attachable evidence that the
operator-run remote pass completed end-to-end (mirroring M1.2; it makes deliverable-1 acceptance
falsifiable rather than a verbal "it ran").

### Preflight (clean skip contract)

The remote spine preflights to a clean skip — each with the exact fix string — unless **all**
of the following are present:

- **Provider config** (read by `providers/remote_libvirt/config.py` at runtime):
  `KDIVE_REMOTE_LIBVIRT_URI` + the three TLS cert refs (`KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF` /
  `_CLIENT_KEY_REF` / `_CA_CERT_REF`) + `KDIVE_REMOTE_LIBVIRT_GDB_ADDR` (the ACL'd gdbstub listen
  address — it has **no default** and remote provisioning fails closed without it, so the
  preflight requires it to keep the clean-skip contract rather than failing at the provision
  phase). The URI is the opt-in gate (`is_remote_libvirt_configured()`).
- **Stack reachability**: the OIDC issuer reachable, `KDIVE_STACK_BASE_URL` set,
  `KDIVE_DATABASE_URL` set.
- **A test/runbook input** — `KDIVE_REMOTE_BASE_IMAGE_VOLUME`: the operator-staged base-image
  volume name the spine feeds into the provision profile's `base_image_volume` field. This is a
  *test* env var (the e2e's input to the profile factory), **not** part of the
  `KDIVE_REMOTE_LIBVIRT_*` provider-config surface — the runbook labels it as such.

The RBAC-negative wire checks (viewer denied an operator op; project-only token denied the
all-projects report) need only issuer + stack — they fire in the auth layer before any
provisioning — so they preflight on issuer+stack alone, exactly as the local spine's
`_wire_preflight` does. These run identically against either provider, so they stay owned by
the local test; the remote test does **not** duplicate them.

### CI-verifiable surface (TDD target)

The spine body itself only runs against real hardware. The **non-gated** behavior this PR can
test in normal CI:

1. The extracted `spine.py` `phase` naming contract: a raised exception inside a phase becomes a
   `SpinePhaseError` naming that phase; an inner `SpinePhaseError` is not re-wrapped. (Moves
   with the helpers; the local test's two existing unit tests re-point at `spine.py`.)
2. The remote preflight skip logic: with the remote env unset, `_remote_spine_preflight()` calls
   `pytest.skip` with the actionable `KDIVE_REMOTE_LIBVIRT_URI` / base-image reason — assert via
   `pytest.raises(Skipped)`.
3. The remote provision profile factory validates: `_remote_provision_profile()` parses cleanly
   through `ProvisioningProfile.parse` (catches a profile-shape regression — e.g. the
   disk-image↔remote-section pairing rule — without a host).

## Deliverable 2 — the milestone-end portability report

**Decision:** generate the report from the gate, single source of truth. Extend
`scripts/m2_portability_gate.py` with a `--report` flag that renders the same measurement as a
markdown document (baseline tag, the allowlist, the per-file cumulative-touched-lines table, and
the pass/fail verdict). Add a `just m2-report` recipe that writes
`docs/reports/m2-portability.md`. Commit the generated report as the milestone-end record.

- *Considered & rejected:* a hand-written report doc. Rejected — it drifts from the gate and
  becomes a phantom claim; generating it keeps the recorded numbers honest.
- *Considered & rejected:* a test asserting the committed report equals fresh gate output.
  Rejected — the report is a point-in-time milestone record; the branch's own later commits move
  HEAD's numbers, so a "report == HEAD" test would fight the workflow. The `--report` renderer is
  a pure function of a `touched` dict and **is** unit-tested directly (deterministic markdown).

The `--report` rendering is a pure function over the measured `touched` mapping (no new git
calls beyond what `main()` already runs). Default invocation (no flag) is unchanged: it still
exits 0/1/2 as the per-PR gate.

## Deliverable 3 — the operator runbook

`docs/runbooks/remote-live-stack.md`, mirroring `docs/runbooks/live-stack.md` and pointed to by
it. It covers what the remote host adds over the local stack:

- **Worker → host TLS reachability**: the `qemu+tls://` URI, the client cert/key/CA staged as
  `SecretBackend` refs (`KDIVE_REMOTE_LIBVIRT_*_REF`), server-cert verification on (`no_verify`
  forbidden), and a `virsh -c "$KDIVE_REMOTE_LIBVIRT_URI" list` reachability check.
- **The gdbstub-port ACL**: the per-System gdbstub port range (`KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN/MAX`),
  `gdb_addr` is the ACL'd listen address (no default; fail-closed), reachable **only** from the
  worker pool's source — the ACL *is* the auth (ADR-0079). One System's port is unreachable by
  other tenants.
- **Object-store reachability for the presigned PUT**: the guest must reach the object-store
  endpoint to upload the vmcore on the post-crash reboot (the two-phase retrieve); the worker
  mints time-boxed single-object presigned URLs, no standing credential in any guest.
- **The operator-staged base image**: `KDIVE_REMOTE_BASE_IMAGE_VOLUME` names a qcow2 on the
  remote storage pool carrying qemu-guest-agent + drgn + matching vmlinux/debuginfo (the
  ADR-0078/0079 image-content obligations the operator owns).
- **Running it**: `just test-live-stack` collects the remote spine too; it skips clean unless the
  remote env above is present.

## Acceptance (issue #207)

- The full spine completes on a real remote host under per-project role tokens — encoded as the
  operator-run remote spine driver (run by an operator with the runbook; skips clean elsewhere).
- The portability report shows no provider-specific logic in core / `mcp/tools/*` beyond the
  allowlist — encoded as the committed `docs/reports/m2-portability.md`, generated from the gate.

## Guardrails

`just lint`, `just type` (whole tree), `just test` (`-m "not live_vm and not live_stack"`),
`just m2-gate`, `just check-mermaid` / `docs-check` for the docs. The new e2e must collect and
skip clean under `just test` and `just test-live-stack` on a host with no remote config.
