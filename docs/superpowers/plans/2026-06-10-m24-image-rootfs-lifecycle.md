# M2.4 — Image & rootfs lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Milestone plan, issue granularity.** Each task below maps 1:1 to a `M2.4/N` GitHub issue
> implemented by its own `/work-issue` agent. Per the repo workflow (M1.3/M1.4/M2.x), the
> agent authors the line-level TDD steps for its issue from the interfaces and acceptance fixed
> here. This document is the orchestration spine: file map, interfaces, dependency edges, and
> the falsifiable acceptance each issue must hit.

**Goal:** Turn base-OS/rootfs images into a managed subsystem — Python build planes, a DB-backed catalog as the single source of truth, publish/register with reconciler drift repair, and owner-scoped private uploads with a reconciler-pruned TTL.

**Architecture:** A new `kdive.images` package (provider-agnostic core + per-provider `RootfsBuildPlane`s) writes images to the object store and registers them in a new `image_catalog` Postgres table that replaces the read-only YAML catalog. Operator `build`/`publish` run as an `IMAGE_BUILD` worker job; project members `upload` private images through the ADR-0048 ingest. The reconciler repairs publish drift and prunes expired private images. The agent-facing MCP tool surface is unchanged; image management is a `kdivectl images` operator/author surface.

**Tech Stack:** Python 3.13, psycopg3 (async), Postgres advisory locks, MinIO/S3 object store, libguestfs (`virt-builder`/`virt-make-fs`/`guestfish`), the existing `JobHandlerRegistry`, `Repository`/`StatefulRepository` factories, `Setting` config registry, and the `kdivectl` CLI (`cli/commands/`).

**Spec:** [`../specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`](../specs/2026-06-10-m24-image-rootfs-lifecycle-design.md) · **ADRs:** [0092](../../adr/0092-image-rootfs-lifecycle.md), [0093](../../adr/0093-private-image-uploads.md)

---

## Dependency graph (orchestration order)

```
        /1 catalog table + repo + seed + resolver cutover ─┐
                                                            ├─► /4 publish/register + IMAGE_BUILD job ─┐
  /2 RootfsBuildPlane + local plane (independent) ─────────┤                                           ├─► /7 kdivectl images verbs ─► /8 exit-criterion tests + runbook
  /3 remote plane (independent) ──────────────────────────┘   /5 private upload (after /1,/4) ─────────┤
                                                              /6 reconciler sweeps (after /1,/4) ──────┘
```

- **Wave 1 (parallel):** /1 (catalog track), /2 and /3 (build track — need no DB table).
- **Wave 2 (after /1):** /4.
- **Wave 3 (after /1,/4):** /5 and /6 in parallel.
- **Wave 4:** /7 (after /4,/5,/6), then /8 (last).
- **Single migration owner:** `0023_image_catalog.sql` is authored whole in /1 with the full public + private schema. No other issue adds a migration — avoids the parallel registry conflict the M2.2/M2.3 waves hit.

## File-structure map

| Path | Issue | Responsibility |
|------|-------|----------------|
| `src/kdive/db/schema/0023_image_catalog.sql` | /1 | full `image_catalog` DDL (public + private columns, CHECKs, partial unique indexes) |
| `src/kdive/domain/models.py` | /1, /4 | `ImageCatalogEntry` model, `ImageVisibility`/`ImageState` enums; `JobKind.IMAGE_BUILD` |
| `src/kdive/db/repositories.py` | /1 | `IMAGE_CATALOG = StatefulRepository(...)` |
| `src/kdive/images/__init__.py` | /1 | package marker |
| `src/kdive/images/catalog.py` | /1 | async resolver: `resolve_rootfs(conn, provider, name, *, project)` (public-or-owned, private-shadows-public) |
| `src/kdive/images/seed.py` | /1 | app-level seed: read `FIXTURE_CATALOG_PATH`, register rows (read-only against operator data) |
| `src/kdive/providers/local_libvirt/lifecycle/materialize.py` | /1 | cut `_materialize_catalog_rootfs` over to the async resolver |
| `src/kdive/images/planes/base.py` | /2 | `RootfsBuildPlane` port + `RootfsBuildSpec`/`RootfsBuildOutput` |
| `src/kdive/images/planes/local_libvirt.py` | /2 | libguestfs stages in-process + provenance |
| `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` | /2 | rewire the bash-script consumer onto the plane |
| `src/kdive/images/planes/remote_libvirt.py` | /3 | real remote provisioning disk-image (replaces placeholder digest) |
| `src/kdive/services/images/publish.py` | /4 | two-write publish/register (row-first `pending` → object → `registered`) |
| `src/kdive/jobs/handlers/image_build.py` | /4 | `IMAGE_BUILD` handler (build → validate → publish) |
| `src/kdive/images/validation.py` | /5 | libguestfs guest-contract validator |
| `src/kdive/services/images/upload.py` | /5 | private upload: quarantine → validate → row-first register; per-project quota |
| `src/kdive/reconciler/images.py` | /6 | `leaked_images` / `dangling_images` / `expired_private_images` sweeps |
| `src/kdive/reconciler/loop.py` | /6 | append the three `_RepairSpec`s; extend `ReconcileReport` |
| `src/kdive/cli/commands/images.py` | /7 | `kdivectl images` verb group |
| `src/kdive/config/core_settings.py` | /1,/5 | `IMAGE_*` `Setting`s (publish grace, private lifetime default/max, per-project caps) |
| `scripts/live-vm/*.sh` | /2 | **deleted after** consumers migrate |
| `fixtures/local-libvirt/` | /1 | seeded into DB then **removed** from source tree |
| `docs/runbooks/live-stack.md` | /2 | updated to `kdivectl images build` flow |

---

## Task /1: Catalog table + repository + seed + resolver cutover

**Files:**
- Create: `src/kdive/db/schema/0023_image_catalog.sql`, `src/kdive/images/__init__.py`, `src/kdive/images/catalog.py`, `src/kdive/images/seed.py`
- Modify: `src/kdive/domain/models.py` (model + enums), `src/kdive/db/repositories.py` (`IMAGE_CATALOG`), `src/kdive/providers/local_libvirt/lifecycle/materialize.py` (resolver cutover), `src/kdive/config/core_settings.py` (`IMAGE_PUBLISH_GRACE`)
- Delete: `fixtures/local-libvirt/*.yaml` (after the seed is proven)
- Test: `tests/db/test_image_catalog_migration.py`, `tests/images/test_catalog_resolver.py`, `tests/images/test_seed.py`

**Schema (`0023_image_catalog.sql`):**

```sql
CREATE TABLE image_catalog (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider      text        NOT NULL,
    name          text        NOT NULL,
    arch          text        NOT NULL,
    format        text        NOT NULL,
    root_device   text        NOT NULL,
    object_key    text        NOT NULL,
    digest        text,                       -- qcow2 content digest (image identity)
    capabilities  text[]      NOT NULL DEFAULT '{}',
    provenance    jsonb       NOT NULL DEFAULT '{}',
    visibility    text        NOT NULL CONSTRAINT image_visibility_check
                              CHECK (visibility IN ('public','private')),
    owner         text,                        -- owning project iff private
    expires_at    timestamptz,                 -- required iff private
    state         text        NOT NULL DEFAULT 'pending' CONSTRAINT image_state_check
                              CHECK (state IN ('pending','registered')),
    pending_since timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT image_private_owner CHECK ((visibility = 'private') = (owner IS NOT NULL)),
    CONSTRAINT image_private_expiry CHECK ((visibility = 'private') = (expires_at IS NOT NULL))
);
CREATE TRIGGER image_catalog_set_updated_at BEFORE UPDATE ON image_catalog
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();
-- one registered public image per identity (pending rows excluded so a crashed publish never wedges retry)
CREATE UNIQUE INDEX image_catalog_one_public ON image_catalog (provider, name, arch)
    WHERE state = 'registered' AND visibility = 'public';
-- a project's private image name resolves to exactly one image
CREATE UNIQUE INDEX image_catalog_one_private ON image_catalog (owner, provider, name)
    WHERE state = 'registered' AND visibility = 'private';
```

**Interfaces:**

```python
# images/catalog.py
async def resolve_rootfs(
    conn: AsyncConnection, provider: str, name: str, *, project: str
) -> ImageCatalogEntry | None:
    """Resolve one registered rootfs image visible to `project`.

    Returns the project's private image first (private shadows public on the same
    (provider, name)); otherwise the public image; else None.
    """

# images/seed.py
def seed_entries_from_catalog(path: Path) -> list[ImageCatalogEntry]:
    """Read the operator-configured fixture catalog and return rows to register.
    Read-only against operator data — never deletes the files it read.
    """
```

**Acceptance (falsifiable):**
- The migration applies and `CHECK`/partial-unique constraints reject a private row with NULL owner, a public row with two registered same-identity rows, and admit a `pending` duplicate.
- `resolve_rootfs` returns the private image when a project has one shadowing a public same-name image, and the public image for a different project.
- The seed registers each YAML rootfs as a row and leaves the source files untouched (read-only assertion); `materialize` resolves via the DB after cutover.

**Dependencies:** none (track head). **Commit boundary:** migration+model+repo, then resolver+seed, then YAML deletion (separate commit after seed proven).

---

## Task /2: `RootfsBuildPlane` port + local-libvirt plane (consumer migration)

**Files:**
- Create: `src/kdive/images/planes/base.py`, `src/kdive/images/planes/local_libvirt.py`
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (rewire off the scripts), `tests/integration/test_live_stack.py`, `tests/integration/conftest.py`, `tests/scripts/test_live_vm_fixtures.py`, `docs/runbooks/live-stack.md`
- Delete: `scripts/live-vm/build-guest-image.sh`, `build-busybox-rootfs.sh`, `fetch-fedora-cloud-image.sh` (after consumers migrate)
- Test: `tests/images/planes/test_local_libvirt_plane.py`

**Interfaces:**

```python
# images/planes/base.py
@dataclass(frozen=True, slots=True)
class RootfsBuildSpec:
    provider: str
    name: str
    arch: str
    releasever: str
    packages: tuple[str, ...]
    source_image_digest: str
    capabilities: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class RootfsBuildOutput:
    qcow2_path: Path
    digest: str            # content digest of the produced qcow2
    provenance: dict[str, object]   # pinned inputs + build args

class RootfsBuildPlane(Protocol):
    def build(self, spec: RootfsBuildSpec) -> RootfsBuildOutput: ...
```

**Acceptance (falsifiable):**
- The local plane produces a whole-disk ext4 qcow2 with the normalized fstab / removed crypttab / disabled guest SELinux the scripts produced (assert via `guestfish`/`virt-inspector` on the output).
- `provenance` records the pinned `releasever`, package set, and source-image digest.
- `provisioning.py` and the live-stack tests invoke the plane (no reference to the deleted scripts remains: `rg 'build-guest-image|live-vm/' src tests docs/runbooks` returns nothing).
- Exercised on the operator-run live-stack path (`KDIVE_LIVE_SSH_TARGET`); CI smoke asserts plane wiring only.

**Dependencies:** none (build track head, independent of /1). **Commit boundary:** port+plane, then each consumer rewired, then script deletion last.

---

## Task /3: remote-libvirt plane (real provisioning disk-image)

**Files:**
- Create: `src/kdive/images/planes/remote_libvirt.py`
- Modify: the remote provisioning profile that carries the placeholder digest (ADR-0080)
- Test: `tests/images/planes/test_remote_libvirt_plane.py`

**Interface:** implements `RootfsBuildPlane` from /2 (`build(spec) -> RootfsBuildOutput`), producing the remote provisioning disk-image.

**Acceptance (falsifiable):**
- The plane produces a real image whose `digest` replaces the ADR-0080 placeholder; a test asserts the profile no longer references the placeholder constant.
- Provenance recorded as in /2.

**Dependencies:** the `RootfsBuildPlane` port from /2 (port only, not the local plane). **Commit boundary:** one commit.

---

## Task /4: publish/register two-write + `IMAGE_BUILD` job

**Files:**
- Create: `src/kdive/services/images/publish.py`, `src/kdive/jobs/handlers/image_build.py`
- Modify: `src/kdive/domain/models.py` (`JobKind.IMAGE_BUILD = "image_build"`), the worker plane registrar (bind the handler)
- Test: `tests/services/images/test_publish.py`, `tests/jobs/test_image_build_handler.py`

**Interface:**

```python
# services/images/publish.py
async def publish_image(
    conn: AsyncConnection, store: ObjectStore, *, entry: ImageCatalogEntry, source: Path
) -> ImageCatalogEntry:
    """Row-first two-write: insert/adopt a `pending` row, write the object to the
    image prefix, HEAD-gate, then flip the row to `registered`. Idempotent on
    (provider, name, arch): a re-run adopts the existing `pending` row and re-arms
    `pending_since`.
    """
```

**Acceptance (falsifiable):**
- A successful publish leaves a `registered` row whose object HEADs; resolution returns it.
- A crash injected *after* the `pending` row and *before* the object leaves a `pending` row and an objectless state that a re-run adopts (no unique-violation wedge).
- The `IMAGE_BUILD` handler runs build → guest-contract-validate → `publish_image` and dead-letters on a validation failure with a named category.

**Dependencies:** /1 (table + repo). **Commit boundary:** publish service, then job kind + handler.

---

## Task /5: private upload path (validator + quota + register)

**Files:**
- Create: `src/kdive/images/validation.py`, `src/kdive/services/images/upload.py`
- Modify: `src/kdive/config/core_settings.py` (`IMAGE_PRIVATE_LIFETIME_DEFAULT`, `IMAGE_PRIVATE_LIFETIME_MAX`, `IMAGE_PRIVATE_MAX_COUNT`, `IMAGE_PRIVATE_MAX_BYTES`)
- Test: `tests/images/test_validation.py`, `tests/services/images/test_upload.py`

**Interfaces:**

```python
# images/validation.py
def validate_guest_contract(qcow2_path: Path, *, required: Sequence[str]) -> None:
    """libguestfs-inspect the image; raise CategorizedError(CONFIGURATION_ERROR)
    naming the missing element if the guest agent / kdump / drgn / allowlisted
    helpers are absent.
    """

# services/images/upload.py
async def register_private_upload(
    conn: AsyncConnection, store: ObjectStore, *, project: str, principal: str,
    name: str, provider: str, arch: str, quarantine_key: str, expires_at: datetime,
) -> ImageCatalogEntry:
    """Under the PROJECT lock: enforce the per-project count/bytes quota (fail-closed),
    validate the quarantined object's guest contract, then row-first register the
    private image (promote object → `registered`). Records owner=project, principal for audit.
    """
```

**Acceptance (falsifiable):**
- An image missing the guest contract is rejected with a named reason while still quarantined (never registered).
- An upload that would exceed the per-project count or bytes cap is denied (fail-closed) and audited; two concurrent uploads cannot both pass the cap (held PROJECT lock).
- A registered private image is visible only to its owning project (`resolve_rootfs` for another project returns the public/None).

**Dependencies:** /1 (table), /4 (row-first register + object promote pattern). **Commit boundary:** validator, then upload service + config.

---

## Task /6: reconciler sweeps

**Files:**
- Create: `src/kdive/reconciler/images.py`
- Modify: `src/kdive/reconciler/loop.py` (three `_RepairSpec`s in `_repair_plan`; extend `ReconcileReport` + module docstring)
- Test: `tests/reconciler/test_image_sweeps.py`

**Interfaces (each `Callable[[AsyncConnection], Awaitable[int]]`, modeled on `_repair_abandoned_uploads`):**

```python
async def repair_leaked_images(conn, store, *, grace: timedelta) -> int: ...      # object, no row, past grace → delete object
async def repair_dangling_images(conn, store) -> int: ...                          # row, object HEAD missing past deadline → remove row
async def repair_expired_private_images(conn, store) -> int: ...                   # private & expires_at<now(), reference-guarded + extend-fenced → delete object+row
```

**Acceptance (falsifiable):**
- `leaked_images` deletes an objectless-row… no: an object with no row past `IMAGE_PUBLISH_GRACE`; a `pending` row inside its deadline protects its object (not deleted).
- `dangling_images` removes a row whose object is gone past deadline; leaves an in-deadline `pending` row.
- `expired_private_images` prunes an expired private image; **skips** one still referenced by a non-terminal System (JSONB-containment check on `provisioning_profile`); re-reads `expires_at` under the per-row lock so a concurrent `extend` is honored (not clobbered).
- `ReconcileReport` exposes `leaked_images`, `dangling_images`, `expired_private_images` counts; `now()` evaluated in Postgres (no Python clock).

**Dependencies:** /1 (columns), /4 (publish ordering / `pending` semantics). **Commit boundary:** sweeps, then loop wiring.

---

## Task /7: `kdivectl images` verbs + RBAC

**Files:**
- Create: `src/kdive/cli/commands/images.py`
- Modify: `src/kdive/cli/dispatch.py` (register the group); the service layer for `list`/`delete`/`build`/`publish`/`prune`/`extend`
- Test: `tests/cli/test_images_verbs.py`

**Verbs (authz per spec table):** `list` (RBAC-filtered: public + caller's project's private), `upload`, `delete` (project-scoped; operator cross-project via break-glass), `build`/`publish` (`platform_operator`), `prune --expired`/`extend` (`platform_admin` break-glass via `mcp/tools/ops/breakglass.py`).

**Acceptance (falsifiable):**
- Every verb authenticates as an OIDC principal and is audited under `(principal, operator-cli)`.
- An unprivileged or cross-project invocation is **denied and audited** (proves the authz boundary — the milestone-wide finding mirrored from M2.2).
- Mutating operator verbs route the break-glass path, not the per-allocation gate.

**Dependencies:** /4 (build/publish), /5 (upload/delete), /6 (prune/extend semantics). **Commit boundary:** read verbs, then mutating verbs.

---

## Task /8: exit-criterion proof tests + operator runbook

**Files:**
- Create: `tests/images/test_exit_criteria.py`, `docs/runbooks/image-lifecycle.md`
- Modify: kernel build-plane tests for the #227 regression (`tests/providers/local_libvirt/test_build.py`, `tests/providers/remote_libvirt/test_build.py`)
- Test: this issue *is* tests.

**Acceptance — one test per spec exit criterion:**
1. A no-op kernel patch **fails** patch-applied verification, asserted for both kernel build planes (closes #227 class).
2. Each half-published state (object-no-row, row-no-object) is reconciled — injected and swept.
3. A private upload resolves only within its owning project; an expired private image is auto-pruned; an expired image a non-terminal System references is **not** pruned.
4. A non-conforming upload is rejected (named reason); an over-quota upload is denied — both audited.
5. The local-libvirt rootfs build runs through the Python plane on the operator-run live-stack path (env-gated runbook step, not normal CI).

**Dependencies:** all prior. **Commit boundary:** one test file per criterion group, then the runbook.

---

## Self-review

- **Spec coverage:** Two ingestion paths → /2,/3,/4,/5. Catalog single source of truth → /1. Publish/register two-write → /4. Reconciler drift repair (3 sweeps) → /6. Private uploads (owner-scope, quota, validator, reference guard, extend fence) → /5,/6. Verbs + RBAC → /7. Patch-applied verification → /8. All five exit criteria → /8. No spec section is unmapped.
- **Type consistency:** `RootfsBuildPlane.build(spec) -> RootfsBuildOutput` used identically in /2,/3,/4; `ImageCatalogEntry` from /1 used in /4,/5,/6; `resolve_rootfs(...)` signature stable across /1,/5,/6; `JobKind.IMAGE_BUILD` defined once in /4.
- **Single migration owner:** `0023` authored only in /1 with the full schema (no second migration in /5/6).
- **Placeholder scan:** no TBD/TODO; every task carries concrete paths, signatures, SQL, and falsifiable acceptance. Per-task line-level TDD steps are authored by each issue's `/work-issue` agent (stated in the header), consistent with the repo's milestone workflow.
