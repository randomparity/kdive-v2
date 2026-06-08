# Local-libvirt portable fixture design

- **Date:** 2026-06-07
- **ADR:** [`../../adr/0065-provider-component-references.md`](../../adr/0065-provider-component-references.md)
- **Status:** Draft
- **Scope:** Provider fixtures, component references, rootfs/catalog handling, and config
  requirements for portable local-libvirt demos and future production-shaped providers.

## 1. Problem

The current live-stack setup is close to a working local demo, but the fixture model is still
too host-specific. Rootfs images are expected to exist at local paths, `config_ref` is treated as
a complete local `.config`, and `/configs/kdump.config` appears in tests without a prepared
fixture. That is enough for a one-off host, but not for a portable setup that can become the
model for production providers.

The immediate demo is a local-libvirt agent debugging workflow:

1. Build a kernel from an agent-chosen config.
2. Boot it with an agent-chosen command line.
3. Observe and inspect redacted evidence.
4. Patch source.
5. Rebuild and verify.

The long-term goal is that agents use advertised MCP tools directly. Fixture metadata should
prepare reusable provider inputs and advertise requirements; it must not become a hidden workflow
engine or a case-file automation format.

## 2. Goals

- Make local-libvirt fixture setup portable across KVM/libvirt hosts.
- Keep rootfs images and other reusable components artifact-managed when useful, while still
  allowing local-only components for a local provider.
- Let agents decide the full kernel `.config`, kernel image, initrd, rootfs, and command line.
- Let providers validate that agent-supplied components satisfy the selected profile before use.
- Scope catalog listings and profile definitions to the provider that can actually consume them.
- Support future remote providers, where local agent files must be uploaded as private artifacts
  before the remote provider can consume them.
- Keep `docs/test-cases/*.md` as human-operator notes only.

## 3. Non-goals

- No dcache-specific automation schema.
- No tool that parses `docs/test-cases/*.md`.
- No requirement that local-libvirt uploads every local component to S3.
- No hidden provider-side config merge that replaces the agent's config decision.
- No immediate remote-libvirt implementation.
- No immediate replacement of every existing build-profile API in this design document.

## 4. Architecture

The fixture model has four layers:

1. **Provider fixture bundle**: versioned metadata and helper commands for preparing a provider
   host.
2. **Component references**: a common model for local files, private artifacts, and provider
   catalog entries.
3. **Provider/profile requirements**: additive requirements a component set must satisfy.
4. **Provider materialization and validation**: provider-owned staging, cache, and compliance
   checks before boot/build/debug operations.

Containers remain the right mechanism for generic backing services such as Postgres, MinIO, and
mock OIDC. The local-libvirt provider remains host-local because KVM, libvirt, QEMU file access,
SELinux labels, and disk paths are host facts.

## 5. Component References

Provider-consumed components use one reference model. The reference says where the component can
be found; the provider decides whether that reference kind is valid for its runtime.

```yaml
kind: local
path: /absolute/provider-visible/path
sha256: sha256:<hex>
```

```yaml
kind: artifact
artifact_id: <uuid>
sha256: sha256:<hex>
```

```yaml
kind: catalog
provider: local-libvirt
name: fedora-kdive-ready-43
```

These references can describe:

- kernel image
- initrd image
- rootfs image
- complete `.config`
- patch
- `vmlinux` / debuginfo
- future kernel source snapshots

`local` means local to the provider runtime, not local to wherever the agent happens to run. For
`local-libvirt`, that can be the user's KVM host. For a remote provider, arbitrary agent-local
paths are invalid.

Local references are accepted only under provider-declared component roots. The provider resolves
the path, rejects symlink escapes, verifies it is a regular file, checks readability from the
provider runtime, and compares the digest when supplied. A linked local component is scoped by
the registering project or host policy; the mere existence of a file on the provider host does
not make it visible to every project.

Provider support is advertised explicitly:

```yaml
local-libvirt:
  accepted_component_sources:
    rootfs:
      - local
      - artifact
      - catalog
    kernel:
      - local
      - artifact
    initrd:
      - local
      - artifact
    config:
      - local
      - artifact
remote-libvirt:
  accepted_component_sources:
    rootfs:
      - artifact
      - catalog
    kernel:
      - artifact
    initrd:
      - artifact
    config:
      - artifact
```

Unsupported component/source pairs are rejected before provider work is enqueued.

## 6. Rootfs Catalog

Rootfs catalog entries are provider-scoped. A catalog entry is not merely "a qcow2 exists"; it is
"this provider can materialize and use this image."

Example artifact-backed entry:

```yaml
provider: local-libvirt
name: fedora-kdive-ready-43
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: artifact
  artifact_id: 00000000-0000-0000-0000-000000000000
  sha256: sha256:<hex>
visibility: public
capabilities:
  - kdive-ready-console
  - ssh
  - drgn
provider_compat:
  - local-libvirt
```

Example local-only entry:

```yaml
provider: local-libvirt
name: fedora-debug-local
arch: x86_64
format: qcow2
root_device: /dev/vda
source:
  kind: local
  path: /var/lib/kdive/rootfs/local/fedora-debug.qcow2
  sha256: sha256:<hex>
visibility: project
capabilities:
  - kdive-ready-console
```

Catalog listings must be scoped:

```text
rootfs.catalog_list(provider="local-libvirt")
rootfs.catalog_get(provider="local-libvirt", name="fedora-kdive-ready-43")
```

S3-backed entries are supported but not required for local-libvirt. Agents/operators may create
or link local-only images for the local provider after validation. Production policy can later
require artifact-backed entries for selected providers without changing the model.

Provider scope is not the whole access-control story. Catalog entries have visibility:
public/operator-published, project-scoped, or host-policy local. `catalog_list` returns only
entries visible to the caller's project and role. Provider operations re-check the resolved
catalog entry before use, and a provider cache hit never grants access by itself.

## 7. Local-Libvirt Materialization

The object store can be the source of record for reusable rootfs images, but QEMU still needs a
provider-local file path. Local disk is therefore a cache/staging detail owned by the provider.

For artifact-backed rootfs entries:

1. Resolve the catalog or artifact ref to metadata.
2. Derive the cache path from digest:
   `/var/lib/kdive/rootfs/cache/sha256/<hex>.qcow2`.
3. If present, verify regular file, size when known, sha256, and qcow2 format.
4. If absent or invalid, download to `<path>.part`.
5. Verify sha256 and qcow2 metadata.
6. Set mode/label/readability for QEMU.
7. Atomically rename into the cache.
8. Create per-System overlay under `/var/lib/kdive/rootfs/overlays/<system-id>.qcow2`.

For local-backed entries, the provider validates the path and creates an overlay from that base.
The provider may use the local path directly when safe, but it must still validate that QEMU can
read the image and that the declared digest matches when supplied.

Cache keys are content-addressed. Updating a named catalog entry to new bytes creates a new cache
path. Cache entries can be deleted and rebuilt because the artifact registry or linked local path
is the source of truth.

## 8. Config Model

Stored configs in provider fixtures are not complete kernel configs. They are additive
requirements for a provider/profile.

The agent chooses or builds the full `.config`. The provider/build worker verifies the complete
config satisfies the selected profile requirements after normalization.

For KDIVE-managed builds, the build pipeline owns normalization: stage the agent's complete
config, run the target tree's config update step, and validate the effective config before
producing boot artifacts. For externally built artifacts, upload/finalization must validate a
supplied effective `.config` or equivalent build metadata before accepting the kernel image for a
profile. Profile requirement fragments are published for agents to merge; provider-side implicit
merge is not part of this design.

Example profile requirement:

```yaml
profile: console-ready-x86_64
provider: local-libvirt
requires:
  config:
    required:
      CONFIG_SERIAL_8250_CONSOLE: y
      CONFIG_VIRTIO_BLK: y
      CONFIG_VIRTIO_PCI: y
  cmdline:
    required_tokens:
      - console=ttyS0
      - root=/dev/vda
    protected_prefixes:
      - console=
      - root=
      - crashkernel=
  rootfs:
    format: qcow2
    root_device: /dev/vda
    capabilities:
      - kdive-ready-console
```

This supersedes the current build-checkout assumption that `config_ref` is the profile's complete
`.config`. Future build inputs should distinguish:

- agent-provided complete `.config`
- provider/profile config requirements
- validation result

For local-libvirt, the complete `.config` can be a local file or an artifact. For remote
providers, the complete `.config` must be artifact-backed or otherwise provider-accessible.

## 9. Provider Validation

Providers validate components before use. Validation is both a preflight convenience and a
runtime safety boundary; the actual provider operation must revalidate.

Required checks include:

- `.config`: selected profile requirements are satisfied after normalization.
- local paths: references resolve under provider-allowed roots, do not escape by symlink, and
  remain project/host-policy allowed.
- kernel image: bootable image for the provider architecture.
- initrd: readable image and required capabilities when the profile needs them.
- command line: provider-required tokens are present; protected platform tokens are not
  overridden by agent input.
- rootfs: provider-compatible image, valid format, readable by provider runtime, checksum matches
  when declared.
- `vmlinux`: matches the booted kernel build-id when required for debug or postmortem.

For local-libvirt, validation happens against provider-local files or downloaded artifact cache
files. For remote providers, validation happens after the provider service materializes artifacts
on its own host.

## 10. Artifact Uploads And Privacy

Artifacts are the portable transport layer. Local-libvirt does not require upload for local
components, but remote providers do.

Rules:

- User-uploaded components are private by default.
- Uploaded contents are scoped to the user/project that uploaded them.
- Shared catalog entries are explicitly published; project catalog entries stay project-scoped.
- Provider workers resolve authorized artifact refs through KDIVE, not through raw S3 keys given
  to agents.
- S3 object keys are implementation details.
- Remote providers reject arbitrary local paths and consume only authorized artifact/catalog
  references.
- Local providers may use local paths directly or download artifact-backed components into a
  provider cache.

Authorization is checked at tool admission and again at worker execution against the job's
authorizing project. Jobs persist component artifact ids, expected digests, and owner context,
not provider-readable object-store keys. A provider cache hit does not grant access unless the
operation is authorized to use the component reference that maps to those cached bytes.

## 11. Fixture Bundle

The fixture bundle prepares a provider host and advertises reusable inputs. It is not the agent's
debug workflow.

Suggested layout:

```text
fixtures/local-libvirt/
  manifest.yaml
  rootfs/
    fedora-kdive-ready-43.yaml
  profiles/
    console-ready-x86_64.yaml
    host-dump-x86_64.yaml
    gdbstub-x86_64.yaml
  configs/
    console-ready.required.config
    host-dump.required.config
    gdbstub.required.config
```

`manifest.yaml`:

```yaml
schema_version: 1
provider: local-libvirt
requires:
  commands:
    - virsh
    - qemu-img
    - virt-builder
    - virt-make-fs
  host:
    kvm: true
    libvirt_uri: qemu:///system
storage:
  allowed_component_roots:
    - /var/lib/kdive/rootfs
    - /var/lib/kdive/components
  cache_dir: /var/lib/kdive/rootfs/cache
  overlay_dir: /var/lib/kdive/rootfs/overlays
  pool:
    name: kdive
    target: /var/lib/kdive/rootfs
rootfs:
  - rootfs/fedora-kdive-ready-43.yaml
profiles:
  - profiles/console-ready-x86_64.yaml
```

The fixture may include a CLI for operators:

```text
kdive fixture local-libvirt prepare
kdive fixture local-libvirt verify
kdive fixture local-libvirt env
```

or initially:

```text
scripts/fixtures/local-libvirt prepare
scripts/fixtures/local-libvirt verify
scripts/fixtures/local-libvirt env
```

Responsibilities:

- prepare host directories and libvirt storage pool
- configure provider-allowed component roots
- build, link, or import rootfs images
- optionally upload/import artifact-backed components into S3
- register local provider catalog entries with visibility metadata
- print exact env for host processes
- verify KVM/libvirt/QEMU readability
- verify linked local paths remain inside allowed roots
- optionally smoke boot a selected profile to `kdive-ready`

The fixture CLI must not parse or execute test-case documents.

## 12. MCP Surface

The agent path should use normal MCP tools. Fixture setup may add discoverable provider and
component tools, but no case-specific orchestration tool.

Candidate discovery tools:

```text
rootfs.catalog_list(provider="local-libvirt")
rootfs.catalog_get(provider="local-libvirt", name="fedora-kdive-ready-43")
profiles.catalog_list(provider="local-libvirt")
profiles.catalog_get(provider="local-libvirt", name="console-ready-x86_64")
```

Candidate component tools:

```text
artifacts.create_component_upload(kind="kernel"|"initrd"|"rootfs"|"config"|"patch"|"vmlinux")
artifacts.link_local_component(provider="local-libvirt", kind="rootfs", path="...", sha256="...", visibility="project")
artifacts.get_component(component_id)
```

Candidate validation tool:

```text
providers.validate_components(
  provider="local-libvirt",
  profile="console-ready-x86_64",
  components={...}
)
```

The validation tool is useful before expensive operations, but the provider operation remains the
enforcement point.

## 13. Current Documentation To Supersede

This design conflicts with the current complete-config assumption in:

- `docs/adr/0053-build-checkout-seam.md`: `config_ref` local-only and complete `.config`.
- `docs/superpowers/specs/2026-06-06-build-checkout-seam-design.md`: config is copied verbatim
  and not treated as a fragment.
- `docs/superpowers/specs/2026-06-04-build-plane-design.md`: build profile names the config to
  stage as `.config`.
- `src/kdive/providers/local_libvirt/build.py`: `_stage_config()` copies `config_ref` directly to
  `workspace/.config`.

ADR-0065 supersedes ADR-0053 for component references and config requirements. Runtime behavior
should change only after the implementation updates the build/profile docs and code paths named
above.

## 14. Phased Implementation

### Phase 1: Docs And Static Model

- Use ADR-0065 as the source-of-truth decision record for component references.
- Update build/profile docs to distinguish complete configs from profile requirements.
- Add provider fixture manifest schema docs.
- Keep current runtime behavior temporarily, but mark the superseded assumptions.

### Phase 2: Local-Libvirt Fixture Bootstrap

- Add `fixtures/local-libvirt/manifest.yaml`.
- Add provider profile definitions.
- Add rootfs catalog metadata.
- Add fixture CLI/script for `prepare`, `verify`, and `env`.
- Keep local path support.

### Phase 3: Component References And Validation

- Add typed component ref models.
- Add config, cmdline, rootfs, kernel image, initrd, and `vmlinux` validation logic.
- Add provider capability advertisement for accepted component/source pairs.
- Add local-libvirt materialization for artifact-backed rootfs/initrd/kernel.

### Phase 4: Remote Provider Portability

- Upload local components as private user/project artifacts when targeting remote providers.
- Make remote providers reject local paths.
- Let remote providers download artifacts into their own caches.
- Add artifact authorization and expiry semantics for provider use.

## 15. Testing

Unit tests:

- component ref parsing and validation
- provider support matrix per component/source pair
- config requirement checking against a complete `.config`
- command-line protected-token enforcement
- rootfs cache naming by sha256
- artifact privacy checks
- catalog visibility filtering and provider-operation rechecks

Provider tests:

- local catalog entry resolves to local path or artifact-backed cache
- corrupt local cache is rejected
- artifact-backed rootfs downloads and verifies before overlay creation
- local-only linked image validates before use

Live tests:

- fixture `verify` boots `console-ready-x86_64` to `kdive-ready`
- local-libvirt uses a local-only rootfs
- local-libvirt uses an artifact-backed rootfs
- remote provider rejects local paths and accepts artifact-backed components when that provider
  exists

## 16. Open Questions

- What is the durable schema for linked local components: catalog rows, artifacts rows with local
  source metadata, or a separate provider-components table?
- How should provider-local linked paths be garbage-collected or invalidated?
- Should local-only catalog entries be project-scoped, host-scoped, or both?
- How much kernel-image validation is possible before boot without false confidence?
- Should config normalization be performed by KDIVE, by an agent-invoked helper, or only by the
  build pipeline that receives the complete `.config`?
