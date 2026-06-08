# ADR 0065 — Provider component references and profile requirements

- **Status:** Proposed
- **Date:** 2026-06-07
- **Depends on:** [ADR-0029](0029-build-plane-local-make.md) (local build plane),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (artifact ingestion),
  [ADR-0053](0053-build-checkout-seam.md) (build checkout seam),
  [ADR-0060](0060-per-system-rootfs-overlay.md) (local-libvirt overlays),
  [ADR-0063](0063-typed-provider-runtime.md) (provider runtime seam).
- **Supersedes:** The `config_ref` and remote-reference decisions in
  [ADR-0053](0053-build-checkout-seam.md). ADR-0053 still describes the current
  implemented checkout seam until this ADR's follow-on implementation replaces it.
- **Spec:** [`../superpowers/specs/2026-06-07-local-libvirt-fixture-design.md`](../superpowers/specs/2026-06-07-local-libvirt-fixture-design.md)

## Context

KDIVE needs a portable local-libvirt fixture that can also become the model for future
providers. The current local-libvirt setup assumes several provider inputs already exist as
host-local files. The build checkout seam also treats `config_ref` as a complete local
`.config` copied into the build workspace. That is workable for one host, but it mixes three
different concerns:

- where a component is stored or staged,
- whether the provider runtime can read it,
- whether the component satisfies the selected provider/profile requirements.

Agents should decide the kernel `.config`, kernel image, initrd, command line, rootfs, and
patches they want to use. KDIVE should validate those choices against the selected provider
profile before it builds, boots, captures, or debugs. A local provider can use provider-local
paths directly. A remote provider cannot read an arbitrary agent-local path, so local contents
must be uploaded as authorized artifacts before that provider can consume them.

The backing object store is therefore a portable transport and source-of-record for reusable
or remote-consumed components, not a requirement for every local-only workflow.

## Decisions

### 1. Provider-consumed components use explicit reference kinds

KDIVE will model provider-consumed components with explicit reference kinds:

- `local`: an absolute path visible to the provider runtime;
- `artifact`: a KDIVE artifact reference resolved through authorization and materialized by
  the provider;
- `catalog`: a provider-scoped named entry that resolves to either local or artifact-backed
  source metadata.

These references can describe kernel images, initrds, rootfs images, complete `.config`
files, patches, `vmlinux`/debuginfo, and future source snapshots.

`local` means local to the provider runtime, not local to the agent process. For
`local-libvirt`, a local path may be a file on the KVM/libvirt host. For a remote provider,
an arbitrary local path from the agent machine is invalid.

A `local` reference is not an arbitrary filesystem escape hatch. Each provider defines allowed
component roots and validates that a referenced path resolves to a regular file under one of
those roots, is not a symlink escape, is readable by the provider runtime, and matches its
declared digest when one is supplied. Linked local components are scoped to the registering
project or host policy; they are not globally visible just because the file exists on the
provider host.

### 2. Providers advertise which component source kinds they accept

Provider capabilities include accepted component source kinds per component kind, not only a
single provider-wide set. The expected baseline is:

- `local-libvirt`: `local`, `artifact`, and `catalog` for boot/runtime components it
  implements;
- remote providers: `artifact` and `catalog`, not `local`;
- unsupported component/source pairs are rejected before enqueueing provider work.

This keeps local workflows fast while preserving a portable path for remote providers.

### 3. Rootfs catalog entries are provider-scoped

Rootfs catalog entries are scoped to a provider. A rootfs entry means "this provider can
validate, materialize, and use this image," not merely "these bytes exist."

A local-libvirt catalog entry may be local-backed or artifact-backed. Artifact-backed entries
are downloaded into a provider-local cache and verified before use. Local-backed entries are
validated directly and used as overlay bases when safe.

Catalog listings must be scoped by provider so an agent sees only entries that are meaningful
for the selected provider.

Provider scope is necessary but not sufficient for access control. Catalog entries also carry
visibility: public/operator-published, project-scoped, or host-policy local. `catalog_list`
returns only entries visible to the caller's project and role, and provider operations re-check
the resolved entry before use. A local-only catalog entry can expose a usable component without
making its path or existence available to unrelated projects.

### 4. Local cache is provider state, not the artifact source of record

QEMU/libvirt needs a host-local file path. For artifact-backed components, local-libvirt
materializes the object into a content-addressed cache, verifies it, and creates per-System
overlays from the cached base. The object store remains the source of record for
artifact-backed components. The local cache can be deleted and rebuilt.

Local-only entries remain valid for local-libvirt. They are not silently promoted to S3 unless
the user or operator asks to import or publish them.

### 5. Stored provider configs are additive requirements, not complete kernel configs

Configs stored with provider fixtures or provider profiles describe requirements needed for
that profile to work. They do not replace the agent's chosen kernel `.config`.

The agent may create or select a complete `.config`. The provider/build worker validates that
the complete config satisfies the selected profile requirements after normalization. This
applies to both local and artifact-backed configs.

For a KDIVE-managed build, the build pipeline owns normalization: it stages the agent's complete
config, runs the target tree's normal config update step, then validates the effective config
before producing boot artifacts. For externally built artifacts, the upload/finalization path
must validate either the supplied effective `.config` or equivalent build metadata before the
kernel image is accepted for a profile. A provider profile may publish requirement fragments for
agents to merge, but implicit provider-side merging is not the decision here.

This supersedes ADR-0053's assumption that `config_ref` is the profile's complete local
`.config` copied verbatim to the build workspace.

### 6. Providers validate components before use

Provider operations validate the component set they are about to consume. Validation includes:

- complete `.config` satisfies selected profile requirements;
- local paths resolve inside provider-allowed roots and do not bypass project/host policy;
- command line includes provider-required tokens and does not override protected platform
  tokens;
- rootfs format, checksum, root device, and required capabilities match the profile;
- kernel/initrd are readable and provider-compatible;
- `vmlinux` matches the kernel build-id when debug or postmortem flows require it.

Optional preflight tools may expose these checks to agents before expensive operations, but the
provider operation remains the enforcement point.

### 7. Uploaded components are private unless explicitly published

User-uploaded components are scoped to the user/project that uploaded them. Remote providers
consume authorized artifact refs, not raw S3 keys. Shared catalog entries require explicit
publication or operator registration; project catalog entries remain project-scoped.

Authorization is checked at tool admission and again at worker execution against the job's
authorizing project. Durable jobs persist component artifact ids, expected digests, and owner
context, not provider-readable object-store keys. A provider cache hit never grants access by
itself: the operation must still be authorized to use the component reference that maps to those
cached bytes. This prevents a private artifact's content-addressed cache entry from becoming a
cross-project access path.

This preserves local-only workflows for local-libvirt while giving remote providers a portable
and access-controlled transport.

## Consequences

- Local-libvirt can use locally built kernels, initrds, rootfs images, configs, patches, and
  command lines directly when they are provider-visible, policy-allowed, and valid.
- Remote providers reject local paths and require artifact/catalog references.
- S3-backed rootfs images remain usable by local-libvirt through provider-owned
  materialization.
- Provider fixture configs become requirements and validation inputs, not full kernel configs.
- ADR-0053 remains accurate only for the current implementation until the build profile and
  checkout behavior are replaced.
- The fixture system prepares provider inputs and catalogs; it does not become a debug
  workflow or test-case automation format.

## Considered & rejected

- **Require S3 upload for every local-libvirt component.** Rejected because the provider
  runtime can read local files directly, and forcing upload would slow local workflows without
  improving validation.
- **Keep `config_ref` as the provider's complete config.** Rejected because the agent should
  decide the complete kernel config; provider profiles should only declare requirements.
- **Let remote providers accept agent-local paths.** Rejected because those paths are not
  visible to the remote provider runtime and would create ambiguous, host-dependent failures.
- **Use a global rootfs catalog.** Rejected because compatibility is provider-specific. Catalog
  listings need provider scope so agents do not select images the provider cannot consume.
- **Parse test-case documents as fixture inputs.** Rejected because test-case files are
  human-operator notes. Agents should use advertised MCP tools directly.
