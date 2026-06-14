# ADR 0024 — Provisioning-profile model shape (M0)

- **Status:** Proposed
- **Date:** 2026-06-04
- **Issue:** #15 (M0: Provisioning-profile schema)
- **Depends on:** [ADR-0011](0011-provisioning-profile-schema.md) (the declarative
  profile decision this refines), [ADR-0012](0012-secret-backend.md) (secret
  references), [ADR-0003](0003-six-durable-objects.md) (immutable request inputs),
  [ADR-0009](0009-capability-provider-dispatch.md) /
  [ADR-0022](0022-capability-registry-dispatch-impl.md) (the `resource_kind` seam)
- **Refines:** [ADR-0011](0011-provisioning-profile-schema.md) and the M0 provisioning
  wording in [`../specs/m0-walking-skeleton.md`](../design/m0-walking-skeleton.md)

## Context

[ADR-0011](0011-provisioning-profile-schema.md) decided *that* a provisioning
profile is a versioned, declarative document with a provider-agnostic core and a
provider-specific section keyed by `resource_kind`, with the libvirt variant for
M0. Issue #15 implements that model in `src/kdive/profiles/provisioning.py`. The
ADR leaves the concrete model shape open: how the provider section is keyed in
Pydantic, where the unit boundary sits on the numeric core fields, how the
`configuration_error` failure contract is produced (Pydantic raises its own
`ValidationError`, which is not a `CategorizedError`), how immutability is
expressed, and how the schema version is carried. Those are settled here.

This ADR owns the schema *type* only. It does **not** rewire
`System.provisioning_profile`, which stays `dict[str, Any]` (the stored jsonb
projection) until the provision handler that parses a profile lands — see
decision 6.

## Decision

### 1. The provider section is a nested model with a required, alias-keyed `local-libvirt` field

The wire shape matches ADR-0011's "section keyed by `resource_kind`" literally:

```yaml
provider:
  local-libvirt:
    domain_xml_params: {machine: pc-q35-9.0}
    rootfs_image_ref: oci://registry.internal/rootfs/fedora-40@sha256:…
    crashkernel: "256M"
```

`provider` is a `ProviderSection` model with one field, `local_libvirt`
(`Field(alias="local-libvirt")`), typed `LibvirtProfile` and **required**.
`ProviderSection` sets `extra="forbid"`, so an unknown provider key (e.g.
`cloud:`) is rejected rather than silently dropped. The core validates the
agnostic fields; `LibvirtProfile` validates its own section — the compositional
Pydantic nesting *is* "each provider validates its own section" from ADR-0011.

M0 has exactly one provider kind, so a required single field is the honest shape:
a profile that names no provider cannot be provisioned. A second provider kind in
M1+ adds a field and a "exactly one set" validator at that point, when there is a
real second case to disambiguate — not before.

### 2. Numeric core fields carry explicit units: `vcpu`, `memory_mb`, `disk_gb`

ADR-0011/the issue name the fields `vcpu`, `memory`, `disk`. A bare `memory: int`
is unit-ambiguous (MB? GiB? bytes?), and the discovery seam already standardizes
on `memory_mb` (`providers/local_libvirt/discovery.py`). The profile core uses
`vcpu: int`, `memory_mb: int`, `disk_gb: int`, all `Field(gt=0)`. Aligning the
unit names with the discovery capability set means the value a host advertises and
the value a profile requests are directly comparable without a unit conversion
that an un-suffixed name would hide.

### 2a. `boot_method` is a closed `StrEnum`; M0 ships one value

`boot_method` is a provider-agnostic core field, so it is validated by the core,
not the provider section. It is a closed `BootMethod` `StrEnum` with the single M0
value `direct-kernel` (the install plane's "direct-kernel boot",
`m0-walking-skeleton.md`), mirroring the one-value `ResourceKind.LOCAL_LIBVIRT`
precedent. A closed set means an unknown boot method fails as `configuration_error`
at parse rather than reaching a provider that cannot honor it; new methods (ISO,
PXE) add enum members when their providers land, not speculatively now.

### 2b. Required string fields are non-empty; "present" must mean "usable"

"A missing required field raises `configuration_error`" and "the `crashkernel`
field is present" are the acceptance criteria, but a bare `str` accepts `""` —
present yet useless, and for `crashkernel` that silently defeats the kdump
prerequisite the field exists to guarantee. Every required string field —
`arch`, `kernel_source_ref`, and the libvirt section's `rootfs_image_ref` and
`crashkernel` — is therefore a non-empty `str` (`Field(min_length=1)`) with
`str_strip_whitespace=True` in the model config, so a blank or whitespace-only value
is a `configuration_error`, not a hollow pass. `crashkernel` is otherwise an
**opaque non-empty token** in M0: its grammar (`"256M"`, `"auto"`, range syntax) is
the kernel's, and a format validator here would be brittle and reject valid forms —
non-empty is the contract, and the booted kernel is the real arbiter.
`domain_xml_params` is a map, not a scalar, so its emptiness rule is split out in
decision 2c — `min_length` on a scalar field and on a `dict` field mean different
things and must not be conflated.

### 2c. `domain_xml_params` is `dict[str, str]`, not `dict[str, Any]`

The libvirt section's `domain_xml_params` is
`dict[NonEmptyStr, NonEmptyStr]` with default `{}`. The **map is optionally empty**
(a profile may inject no params); the `min_length=1` rides on **both the key and the
value type**, so any param that *is* present has a non-empty name and a non-empty
value (an unnamed param is as malformed as an empty one). This is
deliberately not `Field(min_length=1)` on the dict itself — that constrains the
entry *count* (it would forbid the `{}` default while doing nothing about value
emptiness), which is the conflation decision 2b warns against. Domain-XML parameters
are text substituted into an XML template, so a string map is the faithful type;
`dict[str, Any]` would admit arbitrary nested structure and is the one core field
whose looseness would contradict the "no inline secrets" posture decision 3 relies
on. The schema models **references** (`rootfs_image_ref`, `kernel_source_ref`) and
opaque text params, not inline secrets; secret *resolution* is
[ADR-0012](0012-secret-backend.md)'s job, so the schema does not scan
`domain_xml_params` for secret material — it constrains the shape, not the meaning.

### 2d. Integer core fields are `strict=True`; malformed types fail closed

A profile is an externally-authored document, so the numeric core fields use
`Field(gt=0, strict=True)`. Without `strict`, Pydantic's lax mode accepts
`vcpu: "4"` (coerced to `4`) and `vcpu: true` (coerced to `1`, since `bool` is an
`int` subclass) — a malformed profile silently becoming a wrong provisioning
request, which violates the "fail fast on malformed input" rule. Strict integers
reject a non-`int` value as `configuration_error` at the parse boundary. The string
fields need no strictness flag (`NonEmptyStr` already rejects blanks and non-strings
fail the `str` type), and `boot_method`/`schema_version` are already closed
(enum / `Literal[1]`), which reject foreign values without coercion.

### 3. The `configuration_error` contract is produced at a parse boundary, not inside the model

Every model in this file sets `extra="forbid"`, which makes Pydantic raise its
native `ValidationError` on an unknown or missing field. The taxonomy
(`ErrorCategory.CONFIGURATION_ERROR`) is produced by a single boundary —
`ProvisioningProfile.parse(data)` — that calls `model_validate` and re-raises any
`ValidationError` as `CategorizedError(category=CONFIGURATION_ERROR)`. This mirrors
the existing boundary pattern in `store/objectstore.py` and
`domain/allocation_admission.py`: the model declares structure; one function maps a
structural failure onto the wire taxonomy. `parse` is the **sanctioned entry
point**; constructing `ProvisioningProfile(**data)` or calling `model_validate`
directly bypasses the mapping and surfaces a raw `ValidationError`, which is a
caller error (the future provision handler parses at the boundary). The model
cannot prevent direct construction; review keeps callers on `parse`.

The error `details` are built from `ValidationError.errors(include_url=False,
include_input=False, include_context=False)`. Excluding `input`/`context` keeps the
**submitted field values out of the error** — a profile may carry references that
resolve to secrets ([ADR-0012](0012-secret-backend.md)) or guest-derived strings,
and the redaction contract forbids echoing them into a response or a log. This
guarantee holds only if **custom validators do not interpolate the submitted value
into their message**: Pydantic copies a validator's `ValueError` text into `msg`,
which `details` keeps even with `include_input=False`. M0's added constraints are
declarative (`min_length`, `gt`, `Literal`, enum membership), whose built-in
messages name the constraint, not the value; any future custom validator must keep
the offending value out of its message text to preserve the guarantee.

### 4. Profiles are frozen (`frozen=True`)

[ADR-0011](0011-provisioning-profile-schema.md) and
[ADR-0003](0003-six-durable-objects.md) make a profile immutable once a System is
created from it (the "immutable request inputs" invariant). The models set
`frozen=True`, so field reassignment raises at the type level rather than relying
on convention. Caveat: `frozen` blocks attribute reassignment but does not deep-
freeze a nested container — `domain_xml_params` (a `dict`) can still be mutated in
place. M0 accepts this; the boundary parses an external document into a fresh model
and does not hand the inner dict back out for mutation, and a deep-freeze wrapper is
not worth its weight for one M0 field. The invariant it most needs to hold —
"the top-level profile a System was created from is not swapped underneath it" — is
enforced.

### 5. The schema version is a required `Literal[1]`

`schema_version: Literal[1]` is required (no default). ADR-0011: "Stored profiles
retain the schema version they were created under; the loader reads prior versions
rather than migrating immutable inputs in place." A required, literal version means
every profile records its version explicitly, and a value M0 cannot read (a future
`2`, or a missing version) fails as `configuration_error` at the parse boundary
rather than being silently coerced. M0 ships exactly version `1`; the loader gains a
version dispatch when a second version exists, not speculatively now.

`Literal[1]` alone is not enough: Pydantic matches `True` and `1.0` against it
(`True == 1.0 == 1`), and `Field(strict=True)` cannot be applied to a `Literal`
schema (it raises at schema-build time). A `mode="before"` field validator therefore
rejects any non-`int` (including `bool`, an `int` subclass) before the `Literal`
coercion can accept it — the same fail-fast posture as the strict integer fields
(decision 2d). The validator's message names the constraint, not the submitted value,
so the redaction guarantee (decision 3) holds.

### 6. Scope: the typed model is added; `System.provisioning_profile` is not rewired

`domain/models.py` types `System.provisioning_profile` as `dict[str, Any]` and
notes the typed model "lands with the issue that owns them." #15 owns and adds the
type, but the issue's *Files* list scopes it to `profiles/` + `tests/profiles/`.
Rewiring `System.provisioning_profile` to `ProvisioningProfile` changes the
repository's jsonb (de)serialization seam and touches every `System` construction
site, which belongs to the provision handler's issue (where a profile is actually
parsed and stored). #15 delivers the schema and its parse boundary; the handler
wires it. The model is not a phantom — its consumer (the provision path) is the
next milestone step, and the parse boundary is the seam it will call.

## Consequences

- The wire format is exactly ADR-0011's `provider: {local-libvirt: {…}}`; an
  unknown provider key or a missing `local-libvirt` section fails closed.
- Unit names match the discovery capability set, so a later admission/fit check
  compares `memory_mb` to `memory_mb` with no hidden conversion.
- Every required field is validity-checked, not just presence-checked: `boot_method`
  is a closed enum, the numeric fields are `gt=0`, and the required strings
  (including `crashkernel`) are non-empty. "Present" means "usable", so the kdump
  prerequisite the `crashkernel` field encodes cannot be satisfied by a blank value.
- The redaction guarantee is bounded and stated: declarative constraints never echo
  the submitted value; a future custom validator must keep the value out of its
  message to preserve it.
- One boundary owns the `ValidationError → configuration_error` mapping; handlers
  get a typed failure and never re-implement the mapping. Error details cannot leak
  submitted values.
- `frozen=True` encodes the immutability invariant at the type level, with a
  documented nested-container caveat rather than a silent gap.
- A required literal version makes every stored profile self-describing and makes an
  unreadable version a clean `configuration_error`, not a coercion.
- `System.provisioning_profile` stays `dict[str, Any]`; the provision-handler issue
  takes the serialization change deliberately. The schema and its tests ship now
  with no cross-cutting ripple.

## Alternatives considered

- **A discriminated union on `resource_kind` for the provider section.** Rejected
  for M0: a tagged union earns its keep with two-plus members, and the wire shape
  ADR-0011 specifies is a *mapping keyed by provider name* (`provider:
  {local-libvirt: …}`), not a list of `{resource_kind, …}` objects. A single-member
  union is a nested model with extra ceremony; the union returns when a second
  provider does.
- **`provider: dict[ResourceKind, LibvirtProfile]`.** Rejected: a bare dict permits
  zero entries or repeated/foreign keys and forces every consumer to re-check
  "exactly one, and it is libvirt." A named required field states the M0 invariant
  in the type and lets `extra="forbid"` reject foreign keys for free.
- **Bare `memory: int` / `disk: int` (the issue's literal field names).** Rejected:
  unit-ambiguous and inconsistent with the discovery seam's `memory_mb`. The suffix
  is the cheapest possible defense against a GiB/MiB mix-up at the admission seam.
- **`boot_method` as a free `str`.** Rejected: an arbitrary string would reach a
  provider that can only honor `direct-kernel` and fail late as a provisioning
  error; a closed enum fails it early as `configuration_error`, which is where an
  unsupported boot method belongs.
- **A `crashkernel` format validator (regex for `256M` / range syntax).** Rejected:
  the grammar is the kernel's and is broad (`auto`, sizes, ranges, conditional
  ranges); a schema-level regex would reject valid forms and rot as the kernel adds
  syntax. M0 requires non-empty and lets the booted kernel be the arbiter.
- **Bare `str` for the required string fields.** Rejected: it makes "present" hollow
  — `crashkernel: ""` would pass while defeating the kdump prerequisite. `min_length=1`
  with whitespace stripping makes presence mean usability.
- **`domain_xml_params: dict[str, Any]`.** Rejected: it admits arbitrary nested
  structure and an inline-secret surface that contradicts the no-inline-secrets
  posture; XML params are text, so a string-valued map is both faithful and strict.
- **`Field(min_length=1)` on the `domain_xml_params` dict itself.** Rejected: on a
  collection `min_length` bounds the entry *count*, so it would forbid the intended
  empty-map default while leaving individual values unconstrained. The value
  non-emptiness rides on the value type (`Annotated[str, Field(min_length=1)]`); the
  map stays optionally empty.
- **Lax (default) integer coercion.** Rejected: lax mode accepts `vcpu: "4"` and
  `vcpu: true` (coerced to `1`), turning a malformed externally-authored profile into
  a silent wrong provisioning request. `strict=True` on the integer fields fails the
  malformed value closed as `configuration_error`, matching the fail-fast posture.
- **Raise `CategorizedError` from inside a model validator.** Rejected: it scatters
  the taxonomy mapping across field validators and fights Pydantic, which wraps
  exceptions raised in validators back into `ValidationError` anyway. One parse
  boundary is simpler and matches the existing object-store/admission pattern.
- **Include submitted input values in the error details** (Pydantic's default).
  Rejected: a profile field may reference or contain secret/guest-derived material;
  echoing it into a response or log violates the redaction contract. Locations and
  messages are enough to fix a malformed profile.
- **Mutable (non-frozen) model.** Rejected: it leaves the ADR-0003/0011 immutability
  invariant to convention. `frozen=True` is a one-line enforcement with a bounded,
  documented caveat.
- **Default `schema_version` to `1`.** Rejected: a default lets an unversioned
  document validate, eroding the "profiles retain their version" guarantee the first
  time one is stored without it. Required is the stronger contract for an immutable,
  long-lived input.
- **Bare `Literal[1]` for the version (no bool/float guard).** Rejected: `Literal[1]`
  coerces `True`/`1.0` to version `1`, tolerating a malformed version the way lax
  integers tolerate `vcpu: "4"`. `Field(strict=True)` is unavailable on a `Literal`,
  so a `mode="before"` validator rejecting non-`int` closes the gap consistently.
- **Type the libvirt model now and rewire `System.provisioning_profile` in #15.**
  Rejected: it pulls the repository jsonb seam and every `System` site into a
  schema-only issue. The provision handler that parses and persists a profile is the
  right owner of that change.
