# Architecture Decision Records

This directory records the load-bearing architecture decisions for the KDIVE
production rewrite. The top-level design (`../specs/top-level-design.md`) lists
nine core decisions and states that each "should become an ADR before
implementation"; those ADRs live here.

## Process

- One decision per file, named `NNNN-kebab-title.md` with a zero-padded,
  monotonic number (`0001`, `0002`, …). Numbers are never reused.
- Copy `0000-template.md` to start a new ADR.
- Open it as **Proposed**, move it to **Accepted** once ratified, and to
  **Superseded by NNNN** when a later ADR replaces it (never edit an accepted
  decision in place — write a new ADR that supersedes it).

## Status lifecycle

```
Proposed → Accepted → Superseded by NNNN
                   ↘ Rejected
```

## Style

The project doc-style guard applies here too: use **Milestone**, not "Sprint",
and keep prose plain and factual (no "critical", "robust", "comprehensive").

## Index

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-greenfield-rewrite.md) | Greenfield rewrite, Python | Proposed |
| [0002](0002-multi-user-mcp-http.md) | Multi-user service; MCP over streamable HTTP | Proposed |
| [0003](0003-six-durable-objects.md) | Six durable objects replace the run-centric model | Proposed |
| [0004](0004-first-slice-local-libvirt.md) | First slice targets local libvirt/QEMU | Proposed |
| [0005](0005-postgres-object-store-state.md) | Postgres + object store for state; advisory locks | Proposed |
| [0006](0006-oidc-rbac-attribution.md) | OIDC/SSO + RBAC with (principal, agent_session) | Proposed |
| [0007](0007-metering-budgets-admission.md) | Metering + budgets/quotas with admission control | Proposed |
| [0008](0008-async-worker-tier-job-queue.md) | Async worker tier + durable job queue | Proposed |
| [0009](0009-capability-provider-dispatch.md) | Capability-based provider dispatch | Proposed |
| [0010](0010-fastmcp-framework-auth.md) | FastMCP server framework + streamable-HTTP auth | Proposed |
| [0011](0011-provisioning-profile-schema.md) | Provisioning-profile schema | Proposed |
| [0012](0012-secret-backend.md) | Secret backend (file-ref for M0) | Proposed |
| [0013](0013-object-store-layout-retention.md) | Object-store layout & retention | Proposed |
| [0014](0014-structured-logging.md) | Structured logging via stdlib `logging` + `contextvars` | Proposed |
