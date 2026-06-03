# ADR 0011 — Provisioning-profile schema

- **Status:** Proposed
- **Date:** 2026-06-03
- **Refines:** the spec's "Provisioning plane" and open follow-up
  "Provisioning-profile schema (how libvirt XML / kickstart / ansible / QCOW2 are
  expressed under one model)".

## Context

The Provisioning plane applies a profile to an Allocation to produce a ready
System (see [0003](0003-six-durable-objects.md)). Provisioners are heterogeneous —
libvirt XML, ISO+kickstart, ansible, golden/QCOW2 images, NIM/PXE — yet they must
be expressible under one model so the plane has a common contract. M0 needs the
libvirt variant only.

## Decision

A provisioning profile is a **declarative document (YAML/JSON) validated by a
versioned Pydantic model**, with a **provider-agnostic core** (target arch, vCPU,
memory, disk, boot method, kernel-source reference) and a **provider-specific
section keyed by `resource_kind`**. M0 implements the **libvirt** variant
(domain-XML parameters + a rootfs image reference). A profile is **immutable** once
a System is created from it (the carried invariant: immutable request inputs).

A profile carries **no inline secrets** — any credential (an image-registry login,
an SSH key) is a reference resolved by [0012](0012-secret-backend.md). Stored
profiles **retain the schema version** they were created under; the loader reads
prior versions rather than migrating immutable inputs in place. The core validates
the provider-agnostic fields; each provider validates its own section.

## Consequences

- One schema spans future provisioners; each adds a provider-specific section
  without changing the core.
- The provider-specific escape hatch keeps the model honest about real differences
  (libvirt XML vs kickstart) instead of forcing a lossy abstraction.
- Profiles are validated at the boundary; an invalid profile fails fast with
  `configuration_error`.
- Immutability means reprovisioning with changes creates a new System, preserving
  the lifecycle rules in [0003](0003-six-durable-objects.md).

## Alternatives considered

- **Raw libvirt XML passthrough.** Rejected: not portable to non-libvirt
  providers; the plane would have no common contract.
- **A fully abstract schema with no provider section.** Rejected: it cannot express
  provider specifics (NUMA, PCI passthrough, kickstart) without leaking them
  anyway.
- **A separate unrelated schema per provider.** Rejected: no shared core, so the
  plane could not validate or reason about profiles uniformly.
