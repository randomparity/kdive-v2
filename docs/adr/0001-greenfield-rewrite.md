# ADR 0001 — Greenfield rewrite, in Python

- **Status:** Proposed
- **Date:** 2026-06-03
- **Deciders:** KDIVE architecture
- **Implements core decision:** #1 in [`../specs/top-level-design.md`](../specs/top-level-design.md)

## Context

The existing `kdive` (~33k LOC at `~/src/kdive-v1`) is a single-user, local,
stdio proof-of-concept. Its central abstraction is run-centric — one run bundles
build + boot + debug — backed by per-run JSON files and `flock`. The production
goal is a multi-user hosted service spanning heterogeneous resources (local VMs,
remote libvirt, bare metal, PowerVM, cloud). Retrofitting tenancy, durable state,
admission control, and a worker tier onto the run-centric core would mean
rewriting its load-bearing assumptions piecemeal while preserving none of their
value.

The PoC nonetheless contains well-tested, portable modules: redaction
(`safety/redaction.py` + `safety/secret_registry.py`), path safety
(`safety/paths.py`), the gdb-MI tier, drgn introspection, crash postmortem,
run-readiness preflight, the `ErrorCategory` taxonomy (`domain.py`), and the
4096-byte `read_memory` cap.

## Decision

We will start the production system as a **greenfield rewrite** rather than an
in-place refactor, keeping the PoC as a reference and a source of portable
modules ported behind the new plane interfaces. The implementation language
remains **Python**, for native access to the kernel-tooling ecosystem (drgn,
libvirt bindings, crash, the MCP SDK).

## Consequences

- The new architecture (six durable objects, capability-dispatched providers,
  worker tier) is designed clean, not bent around run-centric assumptions.
- Every salvaged PoC module needs an explicit port plan and a new home behind a
  plane interface; "port the PoC modules" is itself a deferred follow-up
  (see the spec's Open follow-up decisions).
- Choosing Python forgoes the compile-time guarantees of a typed-systems
  language; the project leans on `ty`, `ruff`, and tests to compensate.
- No dual-running or migration path from the PoC is owed — it is a reference,
  not a predecessor instance to migrate.

## Alternatives considered

- **In-place refactor of the PoC.** Rejected: the run-centric core, JSON+flock
  state, and single-user assumptions are exactly what production must replace;
  incremental refactor would carry their constraints forward at every step.
- **Rewrite in a typed-systems language (Rust/Go).** Rejected: the kernel-debug
  toolchain (drgn, libvirt, crash) is Python-native; reimplementing or FFI-wrapping
  it would dominate the effort for no architectural gain.
