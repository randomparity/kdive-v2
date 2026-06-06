# Crash-capture tiers — design

- **Status:** Draft
- **Date:** 2026-06-05
- **Goal:** Give agents a small, provider-agnostic menu of crash-capture methods so a
  deliberately-crashed System can be observed without first building a production kdump
  guest image. Ship three methods on `local-libvirt` — **console**, **host_dump**, and
  **gdbstub** — and shape the surface so a second provider (remote-libvirt, cloud, HMC) is
  a pure addition.
- **Depends on:** the Retrieve/`vmcore.*` plane ([ADR-0031](../../adr/0031-retrieve-plane-vmcore-postmortem.md)
  — the `Retriever.capture` port, `vmcore.fetch` admission, and `postmortem.*` reads this
  extends), the Connect plane ([ADR-0032](../../adr/0032-connect-plane-gdbstub-debugsession.md) — the
  `gdbstub` transport, RSP probe, and SSRF control reused for Tier 2), the Install/boot plane
  ([ADR-0030](../../adr/0030-install-boot-plane.md) — the `<cmdline>` rendering and
  `_kdump_check` this makes method-conditional), and the Provisioning plane + profile
  ([ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) / [ADR-0024](../../adr/0024-provisioning-profile-model-shape.md)
  — the base domain XML and the provider-namespaced profile this extends).
- **Precedent:** [ADR-0039](../../adr/0039-ssh-transport-live-introspection.md) §1 registers a
  second transport (`ssh`) as another `kind` advertised under the one
  `(connect, open_transport, local-libvirt)` capability — "new transport = provider change only,"
  dispatched by capability match. Capture-method mirrors this: one capture operation, `method` as a
  runtime argument, the provider validates the supported-set.
- **ADR:** [ADR-0049](../../adr/0049-crash-capture-tiers.md) (the decisions this spec settles).

## 1. Problem

The Retrieve plane has exactly one way to get a core off a System: `_real_wait_for_vmcore`
(`providers/local_libvirt/retrieve.py:245`) waits for an **in-guest kdump** file. kdump needs
a kdump-capable guest rootfs (the placeholder-digest A1 gap, `scripts/live-vm/build-guest-image.sh`)
— the single heaviest host prerequisite. Yet the deterministic dcache test case
(`docs/test-cases/05-dcache-dhash-entries-oob-read.md`) only needs the crash *signal* (an oops
or KASAN report naming `__d_lookup()`), which is observable by far lighter means.

Three lighter capture methods need no kdump guest image, and two of them are already
implemented in the proof-of-concept (`~/src/kdive-v1`):

- **console** — serial-console oops/KASAN text; detects the crash and scores an A/B run.
- **host_dump** — `virsh dump --memory-only` reads guest RAM host-side into a drgn-loadable
  ELF; no in-guest tooling.
- **gdbstub** — QEMU `-gdb` + `nokaslr`, attach gdb/drgn live (the Connect plane already does
  the attach).

This spec adds those three as selectable capture methods and decouples the non-kdump path from
the kdump prerequisites. Full in-guest **kdump** (Tier 3) remains the production-fidelity path
and is deferred to [#115](https://github.com/randomparity/kdive/issues/115).

## 2. Scope

**In scope (built now):**

- A provider-agnostic `method` vocabulary: `console` | `host_dump` | `gdbstub` | `kdump`.
- `vmcore.fetch(system_id, method=…)` threading `method` to the capture port; the provider
  validates the method against its supported-set and rejects an unsupported one with
  `configuration_error` (**Light** alignment — no discovery tooling yet).
- A typed `debug` block on the `local-libvirt` profile section: `preserve_on_crash`, `gdbstub`.
- Base domain XML gains an **always-on** serial console with a `<log file=…>` tee; the two
  debug flags add a `pvpanic` device + `<on_crash>preserve</on_crash>` (Tier 1) and a QEMU
  `-gdb` argument (Tier 2).
- A `host_dump` capture seam (`virsh dump --memory-only`) and the method-agnostic
  `read_vmcore_build_id` / `extract_redacted` seams it shares with kdump.
- The `gdbstub` endpoint resolver (`connect.py:_real_resolve_endpoint`).
- Making the install-time kdump preflight (`_kdump_check`) and the profile `crashkernel` field
  **method-conditional**, so a non-kdump boot is not blocked.

**Out of scope (explicitly deferred):**

- Tier 3 in-guest kdump + the A1 kdump guest image → [#115](https://github.com/randomparity/kdive/issues/115).
- A/B comparison/scoring of vulnerable-vs-fixed Runs (gap C2) → next session.
- Patch authoring → build input (`patch_ref`, gap C1) → next session.
- Any non-`local-libvirt` provider implementation. The vocabulary is provider-agnostic; the
  realizations here are local-libvirt only.
- A capture-method **discovery** MCP tool (the Full alignment option) — earns its keep when
  provider #2 lands.

## 3. Capture-method vocabulary and provider alignment

The method enum is defined at the **domain** level (not inside `local-libvirt`), so it is the
one vocabulary agents learn across every future provider. Each method is a *verb*; the provider
maps the verb to a mechanism:

| Method | local-libvirt (this spec) | remote-libvirt (future) | cloud (future) | LPAR/HMC (future) |
|---|---|---|---|---|
| `console` | serial `<log file>` tee | serial log on remote host → fetch | `ec2 get-console-output` | HMC vterm capture |
| `host_dump` | `virsh dump --memory-only` | `virsh dump` remote → retrieve | unsupported | unsupported (HMC system dump differs) |
| `gdbstub` | QEMU `-gdb` loopback | QEMU `-gdb` + ssh tunnel | unsupported | unsupported |
| `kdump` | in-guest → host path (#115) | in-guest → fetch | in-guest → S3 | in-guest → HMC/NFS |

`console` and `kdump` are near-universal verbs with provider-specific transports; `host_dump`
and `gdbstub` are QEMU-specific and absent on cloud/HMC. The agent must therefore **not assume**
a method exists — the provider owns a supported-set and rejects the rest. For `local-libvirt`
the supported-set is `{console, host_dump, gdbstub}` now (kdump joins it via #115).

Provider-specific *options* (the debug flags) live under the existing provider-namespaced
profile section (`provider.local_libvirt.debug`); a future cloud provider adds
`provider.aws.debug` with its own options and needs no realignment.

## 4. Tool surface

The method vocabulary is unified, but each method **dispatches to the plane that realizes it**
(ADR-0049 Decision 1) — it is not one argument on one tool.

**`host_dump` / `kdump` → `vmcore.fetch`** (the core-producing methods). `vmcore.fetch`
(`mcp/tools/vmcore.py:337`) gains an optional `method`:

```python
async def vmcore_fetch(
    system_id: Annotated[str, Field(description="The crashed System whose core to capture.")],
    method: Annotated[
        Literal["host_dump", "kdump"],
        Field(description="Core-producing capture method; the provider rejects an unsupported one."),
    ] = "host_dump",
) -> ToolResponse: ...
```

- The method is recorded on the `CAPTURE_VMCORE` job payload (`{"system_id", "method"}`) and the
  dedup key becomes `{system_id}:capture_vmcore:{method}` so two methods on one System are
  distinct jobs, not deduped into one.
- `capture_handler` (`vmcore.py:202`) passes `method` to `retriever.capture(system_id, method)`.
- A method outside the provider supported-set, or one the System was not provisioned for (e.g.
  `host_dump` without `preserve_on_crash`), is a synchronous `configuration_error` at the tool
  boundary, **before** any job is admitted. `kdump` is rejected until #115 ships it.

**`gdbstub` → the Connect plane** — reached via `debug.*` / `open_transport(system, "gdbstub")`,
a live transport that produces no core (§9). It is not a `vmcore.fetch` method.

**`console` → an artifact read** — the boot/console plane registers the always-on console
`<log file>` as a `redacted` artifact when the boot window closes (ready, crashed, or timed out),
and the agent reads it via `artifacts.*`. It is not a `vmcore.fetch` method and is **not**
`CRASHED`-gated, so the healthy A/B baseline (a non-crashing System) is readable. `postmortem.*`
remains a `host_dump`/`kdump` core reader (it needs a core).

## 5. Provisioning profile changes

`LibvirtProfile` (`profiles/provisioning.py:82`) gains a typed, optional debug block:

```python
class LibvirtDebugOptions(_ProfileBase):
    preserve_on_crash: bool = False  # Tier 1: pvpanic device + <on_crash>preserve> + panic=0
    gdbstub: bool = False            # Tier 2: QEMU -gdb arg + nokaslr

class LibvirtProfile(_ProfileBase):
    ...
    crashkernel: NonEmptyStr | None = None   # was required; now kdump-only
    debug: LibvirtDebugOptions = Field(default_factory=LibvirtDebugOptions)
```

- `crashkernel` becomes optional. It is the kdump (`method="kdump"`) prerequisite only; a
  non-kdump capture does not require it. The tool boundary rejects `method="kdump"` against a
  profile with no `crashkernel`.
- The debug block is typed, not free-form `domain_xml_params` — consistent with the existing
  whitelist of exactly one param (`SUPPORTED_DOMAIN_XML_PARAMS = {"machine"}`,
  `providers/local_libvirt/provisioning.py:38`). The two flags are validated structurally by
  Pydantic; no new entry in the param whitelist.

## 6. Domain XML additions

`render_domain_xml` (`providers/local_libvirt/provisioning.py:200`) builds a minimal domain
today (name, memory, vcpu, `<os>`, one virtio disk, metadata tag) — no console, no panic
device. This spec adds, all via `ElementTree` (no string interpolation, preserving the no-XXE /
no-injection property the module documents):

1. **Always-on console** — a `<serial type="file">` (or `<serial type="pty">` + `<console>`)
   with `<log file="{console_log_path}"/>`, where `console_log_path` is a deterministic
   per-System host path. This is the Tier 0 artifact source and the boot-readiness signal: the
   install plane's `_real_readiness` seam (`install.py:233`, `_await_ready`) is **realized** by
   tailing this log for the readiness marker (the POC `stream_console` pattern) — readiness is the
   console tail, not a separate poll, so the two are one mechanism. That same `_await_ready`
   loop owns the boot window and registers the console artifact in a `finally` around the loop —
   so the crashed/timeout paths, which `_await_ready` reaches by *raising*, still register the
   console when the window closes for any reason (ready, crashed, timeout — §4, §11).
2. **`preserve_on_crash` flag** → a `<panic model="pvpanic"/>` device and
   `<on_crash>preserve</on_crash>`, so a guest panic freezes the domain (state observable as
   crashed) instead of rebooting away. pvpanic fires only on an actual `panic()`, so this is
   paired with the panic-escalation cmdline (§8) — without it an oops/KASAN fault logs and the
   domain never freezes, and host_dump has nothing to capture.
3. **`gdbstub` flag** → the QEMU passthrough namespace
   (`xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0"` on `<domain>`) and
   `<qemu:commandline><qemu:arg value="-gdb"/><qemu:arg value="tcp:127.0.0.1:{port},server=on,wait=off"/></qemu:commandline>`.
   The port is **allocated from a tracked range and persisted on the System**, not derived from a
   hash of `system_id`: a hash over a finite port range collides, and because the Connect resolver
   (§9) keys purely on the System, a collision would let one System's debug session attach to
   *another's* gdbstub on the shared loopback — a cross-System isolation break. To avoid simply
   re-introducing that collision via an allocation race, the port is allocated **atomically under
   the existing provisioning transaction / per-System advisory lock**, persisted on the System row,
   and **released on teardown** (so the range does not leak). The resolver reads the stored value.
   (A per-System `-gdb unix:/…/{system_id}.sock` would sidestep port allocation entirely and is
   filesystem-isolated — viable if the Connect probe is later generalized off TCP; out of scope
   here, where the probe is TCP-only.) Paired with `nokaslr` in the cmdline (§8).

## 7. Capture seam (Tier 1 `host_dump`)

`LocalLibvirtRetrieve.capture` (`retrieve.py:157`) is already method-agnostic: it gets core
*bytes* from a seam, then extracts a build-id, redacts a dmesg derivative, and stores both. Only
the byte-source seam differs by method:

- **`host_dump`** — a new seam `_host_dump_capture(system_id) -> bytes`:
  `virsh -c {uri} dump --memory-only {domain} {tmp_path}`, verify exit status and a leading ELF
  magic (`\x7fELF`), read the bytes, unlink the temp file. Ported from POC
  `local_libvirt_qemu.py` (`virsh dump --memory-only`, ELF-magic validation, partial-file
  cleanup on failure). No transaction held during the dump (mirrors `capture_handler`'s slow
  phase). **Note the state difference from the POC:** the POC dumped a *running* domain, but here
  the domain is `<on_crash>preserve>`-frozen (`VIR_DOMAIN_CRASHED`) at capture — that `virsh dump
  --memory-only` accepts a crashed-state domain is a dependency to verify (§13).
- **`kdump`** — the existing `_real_wait_for_vmcore`, deferred (#115).

`capture(system_id, method)` selects the seam; the rest of the method
(`read_vmcore_build_id` → `_real_read_vmcore_build_id`, `extract_redacted` →
`_real_extract_redacted`, `_put` raw+redacted) is unchanged and shared. The two extraction
seams are **method-agnostic** (they parse an ELF core's notes / run `makedumpfile --dump-dmesg`
or drgn over the bytes regardless of how the bytes were produced) and are implemented here so a
`host_dump` core is fully usable by `postmortem.*`.

## 8. Install-plane changes

`LocalLibvirtInstall.install` (`install.py:141`) currently calls `_kdump_check` unconditionally
and raises `configuration_error` if the kdump path is absent (`install.py:155`). This blocks a
non-kdump boot. Changes:

- The install/boot handler learns the Run's capture method (from the Run/build profile). The
  kdump preflight runs **only** for `method="kdump"`; the three non-kdump methods skip it.
- **Crash-during-boot is success for a `preserve_on_crash` Run, not a boot failure.** The dcache
  bug panics during early boot (path lookups in init), *before* any readiness marker, so
  `_await_ready` would otherwise raise `boot_timeout`/`readiness_failure` and mark the Run failed —
  inverting the intended outcome. For a `preserve_on_crash` Run, `_await_ready` polls domain state
  (restoring the POC's domstate check that console-tail readiness alone drops) and reports a
  **fourth, terminal-success** outcome when the domain enters the crashed state within the boot
  window. The install plane stays DB-free: `_await_ready` *reports* the crashed outcome (a new
  `ReadinessResult` variant) and the boot **handler** records the System as `CRASHED`, then the Run
  proceeds to `host_dump`. This transition is already legal — a System is `READY` when a Run boots
  a kernel on it, and `READY → CRASHED` is an allowed edge (`domain/state.py`, `_TRANSITIONS`), so
  no state-machine change is needed. This is the crash-**during**-boot path; the crash-**after**-ready path
  (boot to readiness, then trigger via a path lookup) reaches `CRASHED` through reconcile instead
  (§10, §13.1). Only a genuine no-crash timeout is `boot_timeout`; for non-`preserve_on_crash`
  Runs, a crashed domain during boot stays a failure.
- The effective cmdline is composed from the Run cmdline plus flag-derived tokens. For
  `preserve_on_crash`, append the **panic-escalation** set, because pvpanic only fires on an
  actual `panic()` and the target faults do not panic by default: `panic_on_oops=1` (a recoverable
  oops alone does not panic), `kasan.fault=panic` when the kernel is KASAN-instrumented (a KASAN
  report otherwise logs and continues), and `panic=0` so the kernel halts at panic for the
  `<on_crash>preserve>` capture instead of rebooting away. For `gdbstub`, append `nokaslr`.
  `console=ttyS0` is always in the Run cmdline (the console artifact). The bug parameter
  (`dhash_entries=1`) rides through unchanged — `_render_direct_kernel_xml` already renders
  `<cmdline>` verbatim (`install.py:222`).

## 9. Connect-plane change (Tier 2 endpoint)

The Connect plane (`connect.py`) is complete except for one stub. `_open_gdbstub` (orchestration),
the loopback-only SSRF control, and the RSP reachability probe (`rsp_reachable`,
`_real_probe`) are implemented. Only `_real_resolve_endpoint` (`connect.py:286`) raises. This
spec implements it: resolve the System's gdbstub endpoint to `("127.0.0.1", port)`, reading the
`port` **allocated and persisted at provision time** (§6.3) — not recomputing a hash, so two
concurrent Systems can never resolve to the same listener. Loopback-only is preserved (the value
is `127.0.0.1`), satisfying the existing `_is_loopback_literal` gate.

## 10. Error handling, state, security

- **Unsupported method** → `configuration_error` at the tool boundary (provider supported-set),
  never a 500, never an admitted job.
- **`host_dump` with no crashed/preserved domain** → the dump seam fails fast; surfaced as the
  existing `readiness_failure`/`infrastructure_failure` contract of `capture`.
- **State gating** — only the core-producing methods go through `vmcore.fetch`, which already
  requires `SystemState.CRASHED` (`vmcore.py:140`). For `host_dump` the System reaches `CRASHED`
  by one of two paths (§8): crash-**during**-boot via the boot handler (from `_await_ready`'s
  crashed outcome), or crash-**after**-ready via pvpanic → `<on_crash>preserve</on_crash>` →
  reconcile. **The reconcile mapping is an assumption to verify** (see §13), not built here.
  `console` (an `artifacts.*` read) and `gdbstub` (a Connect transport) are **not** `vmcore.fetch`
  and **not** `CRASHED`-gated.
- **Security** — all domain-XML additions are constructed with `ElementTree` (no interpolation);
  the gdbstub endpoint is loopback-only (the ported "F2" SSRF control); the console/dmesg
  artifacts pass through the existing `Redactor`; the crash-command allowlist (`postmortem.*`)
  is untouched.

## 11. Testing

Mirroring the plane's existing fakes-over-seams approach (orchestration tested without a host):

- **vmcore.fetch** — `method` (`host_dump`/`kdump`) recorded on the job payload; dedup key
  includes the method; an unsupported or not-provisioned-for method rejected synchronously.
- **console registration** — the boot/console plane registers the console log as a `redacted`
  artifact on every boot-window close (ready, crashed, **and** timed out), so the vulnerable
  branch's pre-readiness oops is captured; the artifact is readable without a `CRASHED` state.
- **capture()** — seam selection by method; `host_dump` seam fake returns bytes → raw+redacted
  rows; ELF-magic rejection path; build-id/redact seams exercised independently of source.
- **render_domain_xml** — console+`<log>` always present; `preserve_on_crash` ⇒ pvpanic +
  `<on_crash>preserve`; `gdbstub` ⇒ qemu `-gdb` arg with the System's persisted allocated port,
  and two Systems get **distinct** ports; neither flag ⇒ neither element (golden-XML assertions).
- **install** — `_kdump_check` skipped for non-kdump methods, enforced for kdump; cmdline
  composition appends the panic-escalation set (`panic_on_oops=1`/`kasan.fault=panic`/`panic=0`)
  for `preserve_on_crash` and `nokaslr` for `gdbstub`; bug param preserved.
- **boot outcome** — a `preserve_on_crash` Run whose domain enters the crashed state mid-window
  yields `_await_ready`'s crashed outcome (not `boot_timeout`/`readiness_failure`), the boot
  handler transitions the System to `CRASHED`, and the console artifact is still registered; a
  non-`preserve_on_crash` Run that crashes mid-boot still fails.
- **connect** — `_real_resolve_endpoint` returns the System's persisted allocated port (matching
  the rendered XML); loopback gate holds.
- **profile** — `crashkernel` optional; `method="kdump"` against a no-`crashkernel` profile
  rejected; typed `debug` block round-trips and rejects unknown keys (`extra="forbid"`).
- **live_vm (gated)** — one end-to-end on the real host: boot v7.0.0 with `dhash_entries=1`,
  observe the console oops (Tier 0), `host_dump` a drgn-loadable core (Tier 1), and attach
  over gdbstub (Tier 2).

## 12. Success criterion

On the local host, for a System booted with `dhash_entries=1`:

1. The **console** artifact read returns the oops/KASAN text naming `__d_lookup()`.
2. `vmcore.fetch(method="host_dump")` produces a drgn/`crash`-loadable core; `postmortem.triage`
   returns a backtrace.
3. The **gdbstub** Connect transport (`open_transport(system, "gdbstub")`) yields a reachable RSP
   endpoint a debugger attaches to.
4. A clean boot (no bad parameter) produces none of the above crash signals — the A/B contrast
   the scoring layer (next session) will consume.

## 13. Open dependencies / assumptions to verify (not built here)

1. **Reaching CRASHED at all.** host_dump needs the System in `SystemState.CRASHED`, which is two
   linked assumptions: (a) the guest actually **panics** — a bare oops or KASAN report does not, so
   the §8 panic-escalation cmdline (`panic_on_oops=1`/`kasan.fault=panic`) is what makes pvpanic
   fire; and (b) the reconcile/discovery path maps a pvpanic-preserved (libvirt "crashed") domain
   to `SystemState.CRASHED`. Both are to be confirmed before implementation, not assumed.
2. **Console artifact owner/key.** The console log is a generic `artifacts.*` object (not a
   vmcore — §4 dispatches it off `vmcore.fetch`), produced by the boot plane. It needs an
   owner/key/kind convention that keeps it discoverable without colliding with `vmcore.list`'s
   `…/vmcore-redacted` filter.
3. **host_dump build-id provenance.** A `virsh dump --memory-only` image carries the build-id in a
   `VMCOREINFO` `PT_NOTE` only if the guest exposes `vmcoreinfo` (the `fw_cfg etc/vmcoreinfo`
   path); the non-kdump boot does not guarantee it. If absent, `_read_vmcore_build_id` cannot
   recover it and `postmortem.crash`'s provenance gate (`retrieve.py`,
   `observed != expected_build_id` → `configuration_error`) rejects the core. Confirm the note is
   present (enable the vmcoreinfo fw_cfg in the boot path) or define a host_dump-specific fallback
   before claiming Tier-1 → drgn/`crash` parity (ADR-0049 Decision 7).
4. **`virsh dump` on a frozen domain.** §7 captures with `virsh dump --memory-only` against the
   `<on_crash>preserve>`-frozen (`VIR_DOMAIN_CRASHED`) domain, whereas the POC dumped a *running*
   domain. Confirm `virsh dump --memory-only` accepts a crashed-state domain; if not, switch the
   freeze to a dump-friendly state (pause-on-panic) or capture via a libvirt crash event instead.
5. The Run→method plumbing (how the install/boot handler learns the capture method) reuses the
   build-profile carrier; the exact field is settled in the implementation plan.
