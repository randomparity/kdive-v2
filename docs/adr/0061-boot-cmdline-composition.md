# ADR 0061 — Boot cmdline composition: platform-required base + appended debug args

- **Status:** Proposed
- **Date:** 2026-06-06
- **Supersedes:** ADR-0056 (removed superseded live-demo driver notes; the build-ledger
  cmdline source stands, and its *replace* semantics are replaced by *append* here).
- **Depends on:** [ADR-0030](0030-install-boot-plane.md) (the direct-kernel `<cmdline>` this
  composes), [ADR-0049](0049-crash-capture-tiers.md)/[ADR-0051](0051-install-method-conditional-crashkernel.md)
  (the per-method `crashkernel=` reservation), [ADR-0060](0060-per-system-rootfs-overlay.md)
  (the `vda` disk the `root=` names).

## Context

ADR-0056 made the Run's recorded cmdline *replace* the entire boot command line, and the
method defaults were `console=ttyS0[ crashkernel=256M]`. Neither carried `root=`, so a
direct-kernel boot could not mount its root filesystem: a vulnerable kernel that crashes in
early init never reached root mount and hid the gap, but the fixed kernel booted with the same
command line and panicked `Unable to mount root fs`. Found driving the live A/B: the only way to
boot the fixed kernel was for the agent to hand-write `root=/dev/vda console=ttyS0` into its
debug cmdline — infrastructure detail the agent should not have to know or be able to omit.

## Decisions

### 1. The platform injects the required args; the Run's cmdline is appended debug args

The boot command line is `system_required_cmdline(method)` followed by the Run's recorded
cmdline. The required base is `console=ttyS0 root=/dev/vda` — the serial console the
readiness/crash classifier tails and the root device provisioning attaches as `vda` — plus
`crashkernel=256M` for a kdump-provisioned System. The Run's ledger cmdline (`runs.build
cmdline=` / external `complete_build`) is the agent's **debug args**, appended after the base.
`DEMO_CMDLINE` is now just `dhash_entries=1`.

### 2. The required args are advertised **and enforced**

`runs.get` returns `required_cmdline` (the resolved base for the Run's System method) so a
cooperative agent reads what the platform will prepend and appends only its trigger. Advertising
alone is advisory, though: because the kernel resolves a duplicate kernel parameter by
last-occurrence, an appended `root=`/`console=`/`crashkernel=` from the agent would *override* the
base. So `runs.build`/`complete_build` also **reject** a cmdline carrying any platform-owned token
(`configuration_error`, `reason: cmdline_overrides_platform_args`) — the agent passes only debug
args; changing the console, root device, or crashkernel reservation is a System/provisioning
concern, not a boot arg. The device in the base tracks provisioning's `target dev`; if that ever
stops being `vda` the two must move together (a consistency test guards it).

### 3. The kdump crashkernel admission check is removed

`runs.install` previously rejected a kdump System whose cmdline lacked `crashkernel=`. The
platform now injects it for every kdump boot, so the check could never fire — it is removed
rather than left as dead validation. A kdump System admits with a debug-only cmdline.
