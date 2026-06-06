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

1. **Provider-agnostic method vocabulary.** Capture methods — `console`, `host_dump`,
   `gdbstub`, `kdump` — are a domain-level enum, a runtime argument to `vmcore.fetch`, not
   per-provider tool names. Agents learn one capture vocabulary; each provider maps a verb to a
   mechanism. This mirrors ADR-0039 (transport kind as a runtime argument behind one capability).

2. **Provider validates its supported-set (Light alignment).** Each provider owns the set of
   methods it supports and rejects an unsupported method with `configuration_error` at the tool
   boundary, before any job is admitted. `local-libvirt` supports `{console, host_dump, gdbstub}`
   now; `kdump` joins it with #115. We do **not** add a capability-discovery MCP tool yet — that
   earns its keep when the second provider lands.

3. **Debug options are typed and provider-namespaced.** Two boot flags
   (`preserve_on_crash`, `gdbstub`) are a typed `debug` block under `provider.local_libvirt`,
   not free-form `domain_xml_params` (consistent with the existing one-key param whitelist). A
   future provider adds its own `provider.<name>.debug`; no realignment.

4. **The console is always on.** The base domain XML gains a serial console with a `<log file>`
   tee at provisioning, unconditionally. Every method benefits from the console artifact, and the
   A/B scoring layer (future) needs it. It is not behind a flag.

5. **Decouple non-kdump capture from the kdump prerequisites.** `crashkernel` becomes optional on
   the profile (kdump-only), and the install-time `_kdump_check` runs only for `method="kdump"`.
   The three non-kdump methods boot without a kdump service.

6. **Tier 3 kdump is deferred.** Full in-guest kdump and the A1 kdump guest image are tracked in
   [#115](https://github.com/randomparity/kdive/issues/115). This ADR delivers Tiers 0–2 only.

7. **host_dump is host-side `virsh dump --memory-only`.** The byte-source seam differs by method;
   build-id extraction and dmesg redaction are method-agnostic and shared, so a host_dump core is
   fully usable by the existing `postmortem.*` path.

## Consequences

- The demo's critical path no longer blocks on the A1 kdump guest image.
- `vmcore.fetch` gains a `method` argument and a method-aware dedup key; the `CAPTURE_VMCORE`
  payload carries the method.
- A guest panic must reach `SystemState.CRASHED` (via pvpanic → `on_crash preserve` → reconcile)
  for `vmcore.fetch` to admit a host_dump; this reconcile mapping is a dependency to verify
  (spec §13), not assumed.
- A second provider is a pure addition: a new supported-set, a new `provider.<name>` profile
  section, and new verb→mechanism realizations — no change to the agent-facing method vocabulary.
