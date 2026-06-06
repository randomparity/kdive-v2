# ADR 0049 — Crash-capture tiers: provider-agnostic method, local-libvirt realizations

- **Status:** Proposed
- **Date:** 2026-06-05
- **Depends on:** [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md) (the `Retriever.capture`
  port, `vmcore.fetch` admission, `postmortem.*` reads this extends),
  [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the gdbstub transport + RSP probe reused for
  Tier 2), [ADR-0030](0030-install-boot-plane.md) (the `<cmdline>` rendering and
  `_kdump_check` this makes method-conditional), [ADR-0025](0025-provisioning-plane-libvirt.md)
  (the base domain XML this extends), [ADR-0024](0024-provisioning-profile-model-shape.md) (the
  provider-namespaced profile this adds a `debug` block to).
- **Precedent:** [ADR-0039](0039-ssh-transport-live-introspection.md) (kind/method as a
  runtime argument behind one capability, provider-validated).
- **Spec:** [`../superpowers/specs/2026-06-05-crash-capture-tiers-design.md`](../superpowers/specs/2026-06-05-crash-capture-tiers-design.md)

## Context

Capturing a crash off a System has exactly one realization today: an in-guest kdump core
(`_real_wait_for_vmcore`). kdump requires a kdump-capable guest rootfs — the heaviest host
prerequisite (the A1 placeholder-image gap). The deterministic dcache test case needs only the
crash signal, which lighter, kdump-free methods provide. Three such methods (console, host_dump,
gdbstub) are already proven in the v1 PoC, and the rewrite's Retrieve/Connect planes are already
seam-shaped to accept them. We will also add remote-libvirt, cloud, and ppc64le/HMC providers
later, on which capture methods are only partially available. These decisions are settled here so
reviews do not re-litigate them.

## Decisions

1. **Provider-agnostic method vocabulary, dispatched per plane.** The capture methods —
   `console`, `host_dump`, `gdbstub`, `kdump` — are one domain-level vocabulary agents learn
   across every provider, but each **dispatches to the plane that realizes it**, not to a single
   tool: `host_dump`/`kdump` produce a core via `vmcore.fetch`; `gdbstub` is a live transport via
   the Connect plane (`open_transport(system, "gdbstub")`), producing no core; `console` is a
   read of the always-on console artifact (Decision 4). The vocabulary is unified; the realizing
   tool is not. Each provider maps a verb to its mechanism, mirroring ADR-0039 (a transport kind
   advertised under one capability, dispatched by capability match).

2. **Provider validates its supported-set (Light alignment).** Each provider owns the set of
   methods it supports and rejects an unsupported method with `configuration_error` at the
   dispatching tool's boundary, before any job or transport opens. M0 has one provider, so the
   supported-set is a `local-libvirt` constant the tool consults directly — provisioning is not
   capability-registry-dispatched in M0 (the `Provisioner` Protocol defers that), and
   registry-resolved supported-sets arrive with provider #2. `local-libvirt` supports
   `{console, host_dump, gdbstub}` now; `kdump` joins it with #115. No capability-discovery MCP
   tool yet — it earns its keep at provider #2.

3. **Debug options are typed, provider-namespaced, and declare what a System is provisioned for.**
   Two boot flags (`preserve_on_crash`, `gdbstub`) are a typed `debug` block under
   `provider.local_libvirt` (not free-form `domain_xml_params`, consistent with the existing
   one-key whitelist). They are bound at **provision/boot** time and declare which methods a
   System is provisioned for; the later `vmcore.fetch`/Connect call **selects** among them.
   Selecting a method the System was not provisioned for (e.g. `host_dump` without
   `preserve_on_crash`) is a `configuration_error`, not a silent stall. A future provider adds its
   own `provider.<name>.debug`; no realignment. These flags are read from the System's resolved
   provisioning profile at the dispatching boundary — the same place the supported-set (Decision
   2) is checked.

4. **The console is always on, registered by the boot plane, and read ungated.** The base domain
   XML gains a serial console with a `<log file>` tee at provisioning, unconditionally. The
   boot/console plane — which already tees that `<log file>` and tails it for the readiness
   marker — registers the console log as a `redacted` artifact when the boot window closes for any
   reason (ready, crashed, or timed out), not only on a clean end-of-boot. This matters: the
   vulnerable branch panics during early filesystem lookup *before* readiness, so its oops console
   — the demo's primary signal — is captured only if registration also fires on the crash/timeout
   path. No separate capture call produces it (this is the producer behind `console`'s "artifact
   read" in Decision 1).
   Reading it is **not** gated on `SystemState.CRASHED`: the A/B baseline is the *fixed-kernel*
   System that does **not** crash, so its console (the "clean `ls /proc`" evidence) must be
   readable on a healthy System. Console is therefore a normal artifact read, distinct from the
   crash-gated core capture.

5. **Decouple non-kdump capture from the kdump prerequisites.** `crashkernel` becomes optional on
   the profile (kdump-only), and the install-time `_kdump_check` runs only for `method="kdump"`.
   The non-kdump methods boot without a kdump service. This is an additive/relaxing schema change
   (a document that still carries `crashkernel` parses unchanged; an absent `debug` defaults), so
   no `schema_version` bump — but see Consequences for the `profile_digest` impact.

6. **Tier 3 kdump is deferred.** Full in-guest kdump and the A1 kdump guest image are tracked in
   [#115](https://github.com/randomparity/kdive/issues/115). This ADR delivers Tiers 0–2 only.

7. **host_dump is host-side `virsh dump --memory-only`.** The byte-source seam differs by method;
   build-id extraction and dmesg redaction are intended to be method-agnostic and shared, so a
   host_dump core is usable by the existing `postmortem.*` path. **Dependency to verify, not
   assumed:** a memory-only QEMU dump only carries the build-id in a `VMCOREINFO` `PT_NOTE` if the
   guest exposes `vmcoreinfo` (the `fw_cfg etc/vmcoreinfo` path); the non-kdump boot does not
   guarantee it. If the note is absent, `_read_vmcore_build_id` cannot recover it and the
   `postmortem.crash` provenance gate (`retrieve.py`, `observed != expected_build_id` →
   `configuration_error`) rejects the core. The implementation must confirm the note is present
   (enable the vmcoreinfo fw_cfg in the boot path) or define a host_dump-specific provenance
   fallback before claiming Tier-1 → drgn/`crash` parity.

## Consequences

- The demo's critical path no longer blocks on the A1 kdump guest image.
- `vmcore.fetch` gains a `method` argument (`host_dump`/`kdump` only) and a method-aware dedup
  key; the `CAPTURE_VMCORE` payload carries the method. `gdbstub` and `console` are reached
  through the Connect plane and an artifact read respectively, not `vmcore.fetch`.
- Only the core-producing methods are `SystemState.CRASHED`-gated. A guest panic must reach
  `CRASHED` (via pvpanic → `on_crash preserve` → reconcile) for a `host_dump`; this reconcile
  mapping is a dependency to verify (spec §13), not assumed. Console and gdbstub are not
  CRASHED-gated.
- Adding the defaulted `debug` block changes `profile_digest` (the reprovision `dedup_key`
  factor, computed over `model_dump(by_alias=True)`) for profiles that omit it, so a pre-change
  and post-change submission of the "same" profile hash differently. M0 carries no persisted
  production profiles or dedup keys to collide across the change, so this is accepted as a
  one-time shift rather than versioned; revisit if profiles persist across a schema change.
- A second provider is a pure addition: a new supported-set, a new `provider.<name>` profile
  section, and new verb→mechanism realizations — no change to the agent-facing method vocabulary.
