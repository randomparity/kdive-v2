# ADR 0051 — Install-time capture-method resolution + method-conditional crashkernel gate

- **Status:** Proposed
- **Date:** 2026-06-06
- **Depends on:** [ADR-0049](0049-crash-capture-tiers.md) (the provider-agnostic capture-method
  vocabulary, the `crashkernel ⇔ kdump` coupling in Decision 5, and the typed
  `provider.local_libvirt.debug` block in Decision 3 this reads), [ADR-0030](0030-install-boot-plane.md)
  (the `runs.install` admission gate and `<cmdline>` rendering this makes method-conditional),
  [ADR-0024](0024-provisioning-profile-model-shape.md) (the provider-namespaced profile this
  resolves the method from).
- **Spec:** [`../superpowers/specs/2026-06-05-crash-capture-tiers-design.md`](../superpowers/specs/2026-06-05-crash-capture-tiers-design.md) §8.
- **Closes:** [#116](https://github.com/randomparity/kdive/issues/116).

## Context

ADR-0049 Decision 5 decoupled the non-kdump capture tiers from the kdump prerequisites: on the
profile, `crashkernel` became optional (kdump-only), and at the **provider** layer
(`install.py`) the `_kdump_check` preflight now runs only for `method == CaptureMethod.KDUMP`.
That relaxation shipped (branch `feat/crash-capture-tiers`, commits `74406ae`, `301c1f9`) but is
**unreachable through the MCP tool surface**: `runs.install` (`runs.py`) rejects *any* cmdline
lacking a `crashkernel=` token, unconditionally, before the job is enqueued. The Tier-0 demo
debug args (`dhash_entries=1 panic_on_oops=1` and the empty baseline) carry no `crashkernel=`,
so an agent cannot boot the deterministic dcache test case.

Two further gaps follow from the same root:

- The tool-layer gate is **decoupled from the profile field** — nothing reads
  `LibvirtProfile.crashkernel` at install/boot; only the cmdline string is inspected. The gate
  asserts a kdump prerequisite without consulting whether the System was provisioned for kdump.
- `install_handler` calls `installer.install(system_id, run_id, kernel_ref, cmdline=cmdline)`
  with **neither** the `method` nor the `initrd_ref` param that `301c1f9` added to `install()`,
  so those params are exercised only by tests and the production install path never resolves a
  capture method nor stages an external initrd.

The capture **method** must therefore be resolved somewhere at the tool layer so the gate can be
made method-conditional and the param can be threaded. Where to resolve it is the open decision.

## Decisions

1. **The install method is resolved from the System's resolved provisioning profile, not the
   Run.** A capture method on `local-libvirt` is a *provision/boot-time* property: ADR-0049
   Decision 3 says the `provider.local_libvirt.debug` flags "are bound at provision/boot time and
   declare which methods a System is provisioned for." `runs.install` and `install_handler` map
   that provisioned-for intent to a `CaptureMethod`:
   - `provider.local_libvirt.crashkernel` set (non-empty) ⇒ `kdump` — directly the
     `crashkernel ⇔ kdump` coupling of ADR-0049 Decision 5;
   - else `debug.gdbstub` ⇒ `gdbstub`; else `debug.preserve_on_crash` ⇒ `host_dump`; else
     `console` (the always-on baseline, ADR-0049 Decision 4).

   The resolution reads the profile dict **loosely** (`.get`-navigation, not
   `ProvisioningProfile.parse`), mirroring the existing `_cmdline_for` loose read of
   `run.build_profile`: install needs only the `crashkernel`/`debug` subset, partially-seeded M0
   Systems must not raise, and a future provider's `provider.<name>.debug` adds no coupling here.
   The navigated key is the **alias** the profile is persisted under, not the Python attribute
   name: `system.provisioning_profile` is a `ProvisioningProfile.model_dump(by_alias=True)`
   (`systems.py`), so the section lives at `provider["local-libvirt"]`
   (`ResourceKind.LOCAL_LIBVIRT.value`), **not** `provider["local_libvirt"]`. A loose read against
   the wrong spelling returns `None` silently — making *every* System resolve non-kdump and
   defeating the gate with no error — so the alias is pinned by a test (Decision 4 / the plan)
   that seeds a real `model_dump(by_alias=True)` profile with `crashkernel` set and asserts the
   kdump install is rejected, rather than trusting prose.

2. **The `runs.install` crashkernel gate is method-conditional.** The `crashkernel=` cmdline
   token is required **iff** the resolved method is `kdump`. For `console`/`host_dump`/`gdbstub`
   the gate admits a cmdline without it. This couples the gate to the profile field
   (`crashkernel` set ⇒ token required), closing the decoupling above: a System provisioned for
   kdump still cannot install a cmdline that drops the reservation, and a non-kdump System is no
   longer blocked.

3. **`_DEFAULT_CMDLINE` splits by method.** The single hard-coded default
   (`console=ttyS0 crashkernel=256M`) becomes two: `console=ttyS0 crashkernel=256M` for the
   `kdump` default and `console=ttyS0` for the non-kdump default. `_cmdline_for` selects the
   method-appropriate default when the Run carries no explicit cmdline, so a kdump System with no
   override still satisfies its own gate and a non-kdump System does not inherit a spurious
   reservation.

4. **`install_handler` threads `method` and `initrd_ref` to the provider.** `method` is the
   Decision-1 resolution. `initrd_ref` is read from the build ledger's `(run_id, "build")`
   result (where the external-build lane records the uploaded initrd's object key,
   `runs.py:_finalize_external_build`); it is passed only when present and non-empty (server
   builds and embedded-initramfs kernels record none, so `initrd_ref` stays `None` and no
   `<initrd>` is emitted). A `_FakeInstaller` asserts the forwarded `method`/`initrd_ref`.

5. **No `unsupported-method` reject is added at install in M0.** `kdump ∉ LOCAL_LIBVIRT_SUPPORTED`
   (ADR-0049 Decision 2, joins via #115). A kdump-provisioned System resolves `method == kdump`,
   passes the (now method-conditional) gate when its cmdline carries the token, and is rejected at
   the provider's `_kdump_check` (the `live_vm` stub raises `missing_dependency` until #115 lands).
   The supported-set reject lives at the `vmcore.fetch`/Connect dispatch boundary (ADR-0049
   Decision 2), not the install gate; replicating it here would be speculative surface for a
   method the install path cannot yet realize.

## Consequences

- The Tier-0 demo path is reachable through the agent-facing surface: a bare System
  (no `crashkernel`, no debug flags) resolves `method == console` and admits
  `crashkernel=`-free debug args.
- The gate now reads the System's provisioning profile, so `runs.install` and `install_handler`
  both fetch the System (one extra read each). A Run whose System row is gone fails fast with
  `configuration_error` rather than resolving a method against a missing profile.
- `install_handler` reads the build ledger to recover `initrd_ref`; this is the same
  `_existing_build_result` short read the build finalize already uses, so no new query shape.
- A loose read that cannot find `crashkernel` resolves to a non-kdump method (fail-open on the
  kdump prerequisite). Two distinct sources of "cannot find" exist and are handled differently:
  a **malformed profile** is unreachable in production (`systems.define`/`update` persist a parsed
  `ProvisioningProfile.model_dump`, so the field is well-formed); a **mis-spelled navigation key**
  (`local_libvirt` instead of the `local-libvirt` alias) is an ordinary code bug that the loose
  read would hide on every System, not just malformed ones — that is why Decision 1 pins the alias
  with a test rather than relying on the field being present. A genuinely kdump-intended System
  with a readable `crashkernel` under the correct alias is unaffected.
- The cmdline still rides through `_render_direct_kernel_xml` verbatim; this ADR does not add the
  flag-derived panic-escalation/`nokaslr` tokens (spec §8, deferred to the Tier-1/Tier-2 plans).

## Considered & rejected

- **Resolve the method from the Run's `build_profile` (a loose `method` key, mirroring
  `cmdline`).** The spec §8's literal wording ("the handler learns the Run's capture method from
  the Run/build profile") suggests this. Rejected: a capture method is a property of how the
  System was *provisioned* (the `debug` flags and `crashkernel` reservation are boot-time domain
  XML, ADR-0049 Decision 3), not a per-build choice; and a `method` key on `build_profile` would
  carry the same reachability wart as `cmdline` (`BuildProfile.parse`'s `extra="forbid"` rejects
  it on the `runs.build` path), so the field would be settable only by direct seeding. Resolving
  from the provisioning profile is both semantically correct and reachable through
  `systems.define`. Spec §8 is reconciled to this wording.
- **Default the method to `host_dump`/`kdump` unconditionally (no profile read).** Rejected: a
  fixed default cannot satisfy both acceptance criteria — admit a non-kdump cmdline *and* still
  reject a kdump install lacking the token — because the gate would have no per-System signal to
  distinguish the two.
- **Full `ProvisioningProfile.parse` in the resolver instead of a loose read.** Rejected: it
  couples install to every unrelated profile field and raises on the partially-seeded profiles M0
  fixtures (and early Systems) carry; the loose read of the opaque profile dict matches the
  established `_cmdline_for` boundary convention. Production profiles are parsed at
  `systems.define`, so the loose read sees well-formed values.
- **Add an `unsupported-method` reject at the install gate.** Rejected as speculative (Decision 5):
  `kdump` is unrealizable at install until #115, the provider's `_kdump_check` already fails it
  honestly, and the supported-set boundary is `vmcore.fetch`/Connect per ADR-0049 Decision 2.
