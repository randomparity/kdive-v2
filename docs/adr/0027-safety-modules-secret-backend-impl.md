# ADR 0027 — Safety modules & file-ref secret backend (implementation shapes, refines 0012)

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-04
- **Deciders:** kdive maintainers
- **Refines:** [ADR-0012](0012-secret-backend.md) (secret backend — file-ref for M0)

## Context

[ADR-0012](0012-secret-backend.md) fixes the *policy*: secrets are handled by
reference only, behind a pluggable `SecretBackend`; the M0 file-ref backend
resolves a reference to a file **only within an allowlisted secrets root**, and on
resolution **registers the value into the redaction registry before the value is
handed to any subprocess or transport**, with pre-registration output quarantined.

This ADR fixes the M0 *shapes* that realize that policy — the package boundary, the
registry semantics, the path-safety contract, and the backend's resolve-order — so
the later debug/retrieve planes (which depend on this for transcript/vmcore
redaction) build on a stable surface. The three reused modules are ported from the
PoC (`kdive.safety.{redaction,secret_registry,paths}`); `secrets.py` is new.

## Decision

1. **Package boundary.** The modules land under the existing `kdive.security`
   package (which already holds `rbac`/`audit`/`gate`), not a new `kdive.safety`
   package: redaction and secret resolution are part of the same security surface
   as RBAC/audit, and a second top-level security package would fragment it.

2. **Registry semantics (ported verbatim).** `SecretRegistry` is process-scoped,
   thread-safe, and reference-counted per `scope`. `scope=None` registers
   process-globally and is **never evicted** by `release`; a bounded `scope` is
   evicted when the last owner releases it. Empty/`None` values are never stored
   (an empty secret would force-mask every string). `PROCESS_SECRET_REGISTRY` is the
   single process-global instance; both the logging filter and every `Redactor`
   seed from it. A monotonic `version()` lets the logging filter cache a `Redactor`
   and rebuild only on change.

3. **Redaction (ported).** `Redactor` snapshots the registry at construction and
   masks (a) every known secret value by exact-value replacement and (b)
   `key=value`/`key: value` pairs whose key matches the secret-name pattern. It
   recurses through mappings/lists/tuples and treats a `{"sensitive": true, ...}`
   mapping's `path` as a value to mask. The snapshot is point-in-time, so a fresh
   `Redactor` is built per response. `SecretRedactionFilter` is the logging-boundary
   redactor; `redact_url_credentials` strips `user:pass@` userinfo and never raises.

4. **Path-safety contract (scoped port).** Only the primitives the file-ref backend
   needs are ported: resolve-against-allowlisted-root with symlink-escape
   containment, and shell-metachar / control-char rejection. The PoC `paths.py`
   carried run-id/Linux-tree/external-artifact/vmlinux validators that depend on
   modules out of scope for #25 (`SecretReference`, `read_elf_build_id`, run-dir
   confinement); those are **not** ported here — they return with the planes that
   own them. The ported surface is `PathSafetyError` plus a single
   `confine_to_root(path, *, allowed_root)` that resolves `path` (following symlinks
   in existing components) and requires the result to live under the resolved
   allowed root, rejecting shell/control chars first.

5. **`SecretBackend` Protocol + `FileRefBackend` resolve-order.** `SecretBackend` is
   a `typing.Protocol` with a single `resolve(ref) -> str`. `FileRefBackend(root,
   registry)` resolves a file reference by: (a) `confine_to_root` against the
   allowlisted root (a ref escaping the root raises `PathSafetyError` and **no value
   is read**); (b) read the file's contents (stripped of a single trailing
   newline); (c) **`registry.register(value, scope=...)` BEFORE returning** — the
   value is in the redaction registry the instant it leaves the backend, so the
   register-before-return ordering is a structural invariant, not a caller
   convention. An empty file resolves to an empty value, which the registry
   silently drops (consistent with "empty values are never stored").

6. **Quarantine of pre-registration output.** Output produced *before* a secret is
   registered cannot be masked by exact-value replacement. The backend closes the
   window by registering before returning, so a *correct* caller never emits
   unredacted output. The residual risk — output a caller buffers between obtaining
   a ref and calling `resolve` — is handled by the consuming plane (object store,
   marked sensitive) per ADR-0012; it is **not** the backend's responsibility and is
   out of scope for #25, which owns only the resolve-order guarantee.

## Consequences

- The redaction registry and `FileRefBackend` are the single seam every later op
  that resolves a secret passes through; transcripts/vmcore redaction in the
  debug/retrieve planes seed from the same `PROCESS_SECRET_REGISTRY`.
- The register-before-return order is enforced by the backend's own control flow and
  pinned by an ordering test, so a future plane cannot resolve-then-forget-to-register.
- Scoping the `paths.py` port to one `confine_to_root` keeps #25 free of phantom
  validators; the omitted PoC validators are tracked to the planes that use them.
- File-ref trust is the worker host's filesystem permissions (ADR-0012), acceptable
  for M0's single-operator local deployment.

## Alternatives considered

- **New `kdive.safety` package mirroring the PoC.** Rejected: fragments the security
  surface; `kdive.security` already owns RBAC/audit/gate.
- **Port `paths.py` verbatim.** Rejected: pulls in `SecretReference`,
  `read_elf_build_id`, and run-dir validators that #25 does not need — phantom code
  that would fail to import (the deps don't exist in v2) or mislead.
- **Register the value lazily on first redaction instead of at resolve.** Rejected:
  a value handed to a subprocess before any `Redactor` is built would never be
  registered; register-at-resolve closes that window unconditionally.
- **A `register(...)` call the caller makes after `resolve`.** Rejected: makes the
  redaction guarantee a caller convention that a single forgetful call site breaks;
  folding it into `resolve` makes it structural.
- **Strip-all-trailing-whitespace from the file value.** Rejected: a secret may
  legitimately end in a space/tab; stripping only one trailing `\n` matches how a
  key file written by `echo`/an editor is stored without corrupting a value whose
  last byte is significant.
