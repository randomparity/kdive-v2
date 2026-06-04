# Red-team threat model — local-libvirt provider XML surfaces (M0)

**Date:** 2026-06-03 · **Pass 2** (follows the concurrency-core pass,
`2026-06-03-red-team-concurrency-core-threat-model.md`). **Scope:** every place the
local-libvirt provider planes *construct* or *parse* XML — `provisioning.render_domain_xml`,
`install._render_direct_kernel_xml`, and `discovery`'s capability/metadata parsing.
**Method:** falsify each stated XML-safety claim with property-based (`hypothesis`) and
adversarial example tests over the existing libvirt fakes (no live host). Tests:
`tests/adversarial/test_provider_xml.py`.

## Surfaces and invariants attacked

| Surface | Claim under test | Result |
|---|---|---|
| `provisioning.render_domain_xml` | domain XML is *constructed* with ElementTree, so a hostile profile value (rootfs/arch) round-trips as data and cannot inject elements/attributes | ✅ corroborated (hypothesis over markup-injection vectors) |
| `install._render_direct_kernel_xml` | reads `domain.XMLDesc()` (libvirtd output) and rewrites the `<os>` | ⚠️ **XXE inconsistency** — see Finding |
| `discovery._parse_arch` / `_parse_system_id` | libvirtd output is a *trust boundary*: parsed with `defusedxml`; malformed → `unknown`/`None`; an *attack* document raises (fail loud) | ✅ corroborated |

## Finding — install parsed libvirtd XML with stdlib ElementTree (XXE defense-in-depth gap)

`install._render_direct_kernel_xml` parsed the domain's `XMLDesc()` with
`xml.etree.ElementTree.fromstring` under `# noqa: S314 - libvirt's own domain XML, not
untrusted input`. But `discovery` treats the **identical source** (libvirtd-emitted XML)
as a trust boundary and parses it with `defusedxml`, explicitly to neutralize
entity-expansion (billion-laughs). Same threat model, opposite defense.

**Reality (proven):** a `DOCTYPE` + nested-entity domain XML is **expanded** by stdlib
ElementTree (empirically: parsed OK, entities expanded — the billion-laughs seed) while
`defusedxml` raises `EntitiesForbidden`. `test_install_rejects_entity_expansion_in_domain_xmldesc`
fails on the old code (no error raised) and passes after the fix.

**Severity:** low — exploiting it requires host-level libvirt access to define a domain
whose `XMLDesc` carries a DTD (libvirt normally emits none), and the install plane is
`live_vm`-gated. But it is a genuine inconsistency in the codebase's own XXE posture and
a cheap defense-in-depth fix.

**Fix:** `install._render_direct_kernel_xml` now parses with
`defusedxml.ElementTree.fromstring`, dropping the `S314` suppression, and maps a
`ParseError`/`DefusedXmlException` to a clean `install_failure` (previously a raw parser
exception escaped the handler). `test_install_still_accepts_a_benign_xmldesc` guards
against over-rejecting a normal `XMLDesc`.

## Residual / latent (not fixed)

- **Reaper availability via `discovery.list_owned`:** `_parse_system_id` raises (fail
  loud) on an attack metadata document, and `list_owned` does not catch it, so a single
  hostile domain metadata blob makes the whole enumeration raise — which the reconciler
  isolates per-pass (the leaked-domain repair fails that pass and retries). Requires
  host libvirt access to plant; the "fail loud" posture is intentional. Noted, not
  changed.
- **Control chars in profile values:** a profile `rootfs_image_ref` containing a C0
  control byte renders structurally-valid-but-unparseable XML (libvirt would reject it
  at `defineXML`). Profile-schema validation, not an XML-injection issue; out of scope.

## Outcome

No XML-injection or RCE was reproduced — construction is injection-safe and discovery
defends the trust boundary. One defense-in-depth inconsistency (install parsing
libvirtd output without `defusedxml`) was fixed via TDD; a 7-test suite now guards the
construct-and-parse contracts.
