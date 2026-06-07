# ADR 0064 — Expected boot failures + bounded redacted artifact search

- **Status:** Proposed
- **Date:** 2026-06-07
- **Depends on:** [ADR-0019](0019-tool-response-envelope.md) (uniform envelopes and
  references, not raw logs), [ADR-0030](0030-install-boot-plane.md) (the `runs.boot`
  worker step), [ADR-0049](0049-crash-capture-tiers.md) (console artifact capture),
  [ADR-0055](0055-install-readiness-kdump-seam.md) (console readiness/crash classifier).
- **Spec:** [`../superpowers/specs/2026-06-07-expected-boot-failures-artifact-search-design.md`](../superpowers/specs/2026-06-07-expected-boot-failures-artifact-search-design.md)

## Context

Some kernel investigations intentionally boot a kernel into a known failure mode. The
`dhash_entries=1` dcache case is the current motivator: a vulnerable kernel should crash
during boot or early path lookup, while a fixed kernel should reach the `kdive-ready` marker.

ADR-0055 made local-libvirt readiness detect console crash signatures, but `runs.boot` still
has only ordinary success or job failure semantics. Treating every crash as a failed boot hides
the reproduction signal the agent is trying to prove. Treating a grep match as the whole answer
would hide too much debug data. The agent needs both a structured reproduction verdict and a
safe way to inspect the redacted console artifact before choosing the next tool, source change,
or boot configuration.

## Decisions

### 1. Expected boot failures are Run-scoped metadata

A Run may declare an `expected_boot_failure` object at `runs.create`. The expectation is bound
to one build/install/boot attempt, not to the reusable System or provider profile. That keeps a
System available for both vulnerable and fixed A/B Runs and lets different test cases declare
different boot-failure signals.

The first supported expectation kind is `console_crash`. It names a bounded grep-style literal
pattern over the redacted console artifact and an optional description. The pattern is a
reproduction signal, not a diagnosis.

### 2. `runs.boot` records expected crashes as successful reproduction outcomes

An unexpected crash remains a failed boot job with the existing `readiness_failure` category.
When the Run declares an expected boot failure and the console evidence matches it, the boot
step is recorded as succeeded with a structured result such as:

```json
{
  "boot_outcome": "expected_crash_observed",
  "expectation_matched": true,
  "evidence_kind": "console",
  "evidence_artifact_id": "<artifact uuid>"
}
```

This is a workflow verdict only. It does not replace agent inspection of the evidence artifact.

### 3. Evidence remains artifact-backed; envelopes do not carry full logs

`runs.boot` may return or persist small scalars and artifact identifiers. It does not inline the
console log or vmcore transcript into the ordinary response envelope. Console output continues
through the existing redacted artifact path before it is exposed to an agent.

This preserves ADR-0019's reference model while making the evidence discoverable: the boot
result points to the redacted console artifact row the agent can inspect next.

### 4. Agents inspect redacted artifacts through bounded search

Add an `artifacts.search_text` tool for redacted System-owned text artifacts. It accepts an
artifact id, a bounded grep-style literal pattern, bounded before/after context, a bounded match
count, and per-line text caps. The first implementation treats `|` as an OR separator between
literal terms; it does not accept arbitrary regular expressions. It returns matching line
numbers and small context windows. It never returns sensitive artifacts, and it does not provide
a default full-artifact dump.

The first implementation rejects redacted artifacts larger than 1 MiB before fetching them. It
does not add object-store range reads in the same change.

This gives agents an iterative loop:

1. Observe the boot result through `jobs.wait` / `runs.get`.
2. List or follow the evidence artifact.
3. Search the redacted console for symbols, panic text, or subsystem hints.
4. Choose the next MCP tool or source edit based on the inspected output.

### 5. Boot expectation matching reuses the same safety rules

The worker-side expected-crash match uses the same bounded pattern rules as
`artifacts.search_text`. A configured expectation cannot invoke an unbounded or expensive regex
over a large artifact because no regex engine is used. If the expectation is malformed,
`runs.create` rejects it as
`configuration_error`; if the artifact cannot be read at boot-finalization time, the boot step
does not claim a reproduced outcome.

## Consequences

- A deterministic crash-trigger Run can be a successful reproduction without treating all boot
  crashes as success.
- Agents can inspect the actual redacted console output via a search tool instead of trusting a
  hidden boolean.
- Existing raw/sensitive artifact protections stay intact: raw vmcores and sensitive artifacts
  remain unreachable from the artifact read/search tools.
- The first search surface is System-owned artifacts only, matching the current console/vmcore
  artifact access model. Run-owned redacted artifact reads need their own ownership/project
  resolution before they are admitted.
- Large redacted logs need a later range-read design; this ADR does not allow loading a large
  object and slicing it in memory.
- The first implementation adds schema and tool-surface changes. Existing Runs without
  `expected_boot_failure` keep current boot semantics.
- A future full text-read tool can be added if needed, but this decision only commits to bounded
  search because the current demo needs iterative grep-style inspection, not bulk log return.

## Considered & rejected

- **Store expectations in the build ledger.** Rejected because boot failure expectations are not
  build output, and hiding them in the `build` step would make retries and `runs.get`
  inspection harder to reason about.
- **Pass expectations to `runs.boot`.** Rejected because the expectation should survive retries
  and be visible as part of the Run's durable intent.
- **Return the whole console log from `runs.boot`.** Rejected because it violates the envelope
  reference model and makes accidental log exposure easier.
- **Use only a hidden grep match.** Rejected because the agent must be able to inspect the
  redacted artifact and decide what the evidence means before changing kernel source or boot
  configuration.
