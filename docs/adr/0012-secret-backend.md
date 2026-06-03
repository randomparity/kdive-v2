# ADR 0012 — Secret backend (file-ref for M0)

- **Status:** Proposed
- **Date:** 2026-06-03
- **Refines:** the spec's "Cross-cutting concerns" (Secrets by reference) and open
  follow-up "Secret backend (file refs for M0; manager integration later)".

## Context

Cloud credentials, BMC/IPMI passwords, SSH keys, sudo, and HMC tokens must never
appear in requests, state rows, or responses. The worker resolves a reference at
its boundary and registers the resolved value for redaction; only
`(present, source-ref)` is persisted. See the spec's "Cross-cutting concerns".

## Decision

Secrets are handled **by reference only**, behind a pluggable **`SecretBackend`
interface**; only `(present, source-ref)` is persisted. M0 ships a **file-ref
backend** — references resolve to files on the worker host (for example an SSH key
path), **only within an allowlisted secrets root** via the ported path-safety
check, so a reference cannot traverse to an arbitrary host file. On resolution the
worker **registers the value into the ported `PROCESS_SECRET_REGISTRY`** — before
the value is handed to any subprocess or transport — so transcripts and console
output are masked by exact-value replacement; any output buffered before
registration completes is **quarantined** (object store, marked sensitive) until
redacted.

## Consequences

- No secret material lands in Postgres rows, MCP requests, or responses; the
  leakage surface is the worker host only.
- A manager backend (Vault, a cloud secret manager) drops in later behind the same
  interface with no call-site change.
- File-ref trust is scoped to the worker host's filesystem permissions —
  acceptable for M0's single-operator local deployment, revisited before
  multi-tenant remote.
- The redaction-registration step is on the critical path of every op that
  resolves a secret; the quarantine rule covers the pre-registration race.

## Alternatives considered

- **Secrets inline in tool requests.** Rejected: guarantees leakage into logs and
  state.
- **Environment variables only.** Rejected: no per-operation scoping, rotation, or
  reference indirection.
- **Require Vault / a cloud secret manager at M0.** Rejected: premature
  infrastructure; the interface lets it arrive when remote providers do.
