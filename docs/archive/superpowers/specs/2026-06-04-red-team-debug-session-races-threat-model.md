# Red-team threat model — debug-session lifecycle & drgn surfaces (M0)

**Date:** 2026-06-04 · **Pass 6** of the red-team engagement. **Scope:** the Connect /
Debug planes — `debug.start_session`/`end_session` single-attach lifecycle, the gdb-MI /
vmcore / introspect / artifacts tool authz, and the drgn-from-vmcore parsing surface.
**Method:** authz coverage review across every new debug-plane tool, then genuinely
concurrent attack of the single-attach invariants on real Postgres. Tests:
`tests/adversarial/test_debug_session_races.py`.

## Authz coverage — all debug-plane tools are project-scoped (the #11 lesson stuck)

Unlike the older `jobs.*` tools (whose cross-project exposure was closed in PR #57), the
debug plane was built behind merged RBAC and scopes **every** tool to project
membership, returning a not-found-shaped error for a row outside the caller's projects:

| Tool module | Scope check |
|---|---|
| `debug.py` (`start`/`end_session`) | `run.project` / `session.project not in ctx.projects` + `require_role(OPERATOR)` |
| `debug_ops.py` (gdb-MI ops) | `session.project not in ctx.projects` + `OPERATOR` + `live` state |
| `vmcore.py` | `system.project` / `run.project not in ctx.projects` + `OPERATOR` |
| `introspect.py` | `run.project not in ctx.projects` |
| `artifacts.py` | `owner["project"] not in ctx.projects`; a `sensitive` (raw vmcore) id is shaped as not-found — only `redacted` artifacts are returned |

No cross-project read/cancel gap remains in the debug plane.

## Concurrency invariants attacked

| Invariant | Result |
|---|---|
| **single-attach** — any number of `start_session` racing one System leave ≤1 `attach`/`live` row; every loser gets `transport_conflict` | ✅ corroborated (12 concurrent races, two Runs per System) |
| **no transport leak** — exactly the winner's transport stays open; every loser's just-opened transport is closed | ✅ corroborated (tracking connector; `live` set == 1) |
| **idempotent end** — two concurrent `end_session` both succeed, write exactly one `->detached` audit row, close the transport exactly once | ✅ corroborated |

`start_session` opens the gdbstub transport *outside* the per-System advisory lock (a
multi-second RSP probe), then re-checks single-attach + System-ready *under* the lock
before the insert, closing the transport on a lost race (ADR-0032 §6a). The existing
suite covers the conflict with a pre-seeded session on one connection; this pass races
the handlers on separate pooled connections and adds the no-leak assertion.

## Observation (latent, not fixed) — teardown does not detach live sessions

`systems.py::teardown_handler` drives the System `-> torn_down` and destroys the domain
but does **not** detach the System's `live` debug sessions (only `control.py`'s
`force_crash`/reboot path does, via `_detach_sessions`). A session orphaned by a teardown
keeps its `live` row until the reconciler's dead-session sweep
(`_repair_dead_sessions`) reaps it — which only fires once `worker_heartbeat_at` goes
stale (`debug_session_stale_after`, 2 min). During that window a `live` row points at a
destroyed domain; Debug-plane ops against it fail (the transport is dead), so the gap is
fail-safe, not a data-integrity or authz issue.

**Why not fixed here:** this is a design choice (teardown is the slow-domain-destroy
path; session detach is the control plane's), and adding a teardown-time detach is a
behavior change for the maintainer to weigh, not a red-team unilateral. Recorded so it is
a tracked decision: if the 2-minute orphaned-`live` window is unacceptable, have
`teardown_handler` detach the System's sessions under the lock it already holds.

## drgn-from-vmcore (noted, deferred)

The introspect/retrieve planes feed a captured vmcore to drgn. In M0 the real `crash`/drgn
seams are `live_vm`-gated stubs (covered by the supply-chain audit, pass 5), so the
untrusted-vmcore parse path is inert in the shipped config. Re-audit when those seams
leave stub status (a vmcore is attacker-influenceable if the guest kernel is).

## Outcome

No debug-plane defect reproduced: authz is uniformly project-scoped, and single-attach +
no-transport-leak + idempotent-end hold under genuine concurrency. Three concurrent
regression tests added; one latent teardown/session-detach consistency window recorded
for a maintainer decision.
