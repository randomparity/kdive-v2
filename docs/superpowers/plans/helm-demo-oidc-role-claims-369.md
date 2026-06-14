# Plan — Helm demo OIDC role claims (#369)

- **Spec:** [`../specs/2026-06-13-helm-demo-oidc-role-claims.md`](../specs/2026-06-13-helm-demo-oidc-role-claims.md)
- **ADR:** [ADR-0108](../../adr/0108-helm-demo-oidc-role-claims.md)
- **Branch:** `feat/helm-demo-oidc-roles-369` (already checked out)
- **Worktree:** `/home/dave/src/kdive-worktrees/helm-demo-oidc-roles-369` — run every
  command from here.

## Execution mode

Implement directly in this session (chart-only change, tightly coupled across one
template + values + two test files + docs). No subagents. TDD per task:
write/extend the failing test, confirm it fails for the right reason, make the minimal
change, rerun the focused test + guardrails, refactor green.

## Guardrail commands (run before every commit)

```sh
just lint        # ruff check + ruff format --check
just type        # ty
just test        # pytest (helm render suite runs because helm is installed)
```

Before the final push, the full gate:

```sh
just ci          # adds chart-version-check, config-guard, helm lint/render, docs, mermaid
```

`chart-version-check` and `config-guard` are the issue-named gates. Helm is installed in
this worktree; `tests/helm/test_helm_render.py` runs under `just test`. (One-time:
`npm ci` in `.github/scripts/mermaid-check/` so `just check-mermaid` runs locally — CI
installs it itself.)

## Key facts the implementer must honor (from the spec, verified)

- `JSON_CONFIG` claims live at `tokenCallbacks[].requestMappings[].claims`
  (mock-oauth2-server navikt 3.0.3).
- Helm `toJson` marshals a map with **alphabetically sorted keys** (Go `json.Marshal`),
  so render-test assertions on JSON substrings are deterministic.
- `--set` **deep-merges** into the default `demo.oidc.claims` map: `--set
  demo.oidc.claims.roles.demo=viewer` overrides only that leaf and leaves
  `projects`/`platform_roles` at their defaults.
- Parser shapes (must round-trip without `AuthError`): `projects` = array of non-empty
  strings; `roles` = `{project: role}` with `role ∈ {viewer,operator,admin}`;
  `platform_roles` = array of `{platform_admin,platform_operator,platform_auditor}`.
- Default project is `demo` (matches `seed-demo`'s default → has budget/quota).
- `aud:["kdive"]` must always render (config `KDIVE_OIDC_AUDIENCE: kdive`; existing test
  `test_bundled_oidc_pins_audience_kdive`).

---

## Task 1 — Parser round-trip unit test for the default claim set (red first)

**Where it fits:** Acceptance criterion 4 — the default the chart ships must parse
cleanly through the RBAC parser, so a typo in `values.yaml` (e.g. `platform-admin`
instead of `platform_admin`) is caught by a fast unit test, not only at deploy time.

**Files:**
- New: `tests/security/authz/test_demo_oidc_claims.py` (mirror existing authz test
  layout — check `tests/security/authz/` for the package path; create dirs if absent).

**Steps:**
1. Write a test that constructs the **exact** default demo claim set as a Python dict
   (`sub`, `projects`, `roles`, `platform_roles` — the same values that will go into
   `values.yaml`) and feeds it through `kdive.security.authz.context.context_from_claims`.
2. Assert the resulting `RequestContext` has: `principal == "kdive-demo"`,
   `projects == ("demo",)`, `roles == {"demo": Role.ADMIN}`, and `platform_roles ==
   frozenset({PLATFORM_ADMIN, PLATFORM_OPERATOR, PLATFORM_AUDITOR})`.
3. Add a focused negative test: a claim set with an unknown role string raises
   `AuthError` (documents the fail-closed contract the chart default must not trip).
4. To avoid the "hand-copied duplicate drifts from values.yaml" trap, define the default
   claim dict once as a module-level constant in the test and add a comment pointing at
   `deploy/helm/kdive/values.yaml demo.oidc.claims`. (A render-vs-unit cross-check is not
   worth a YAML-parse dependency in a unit test; the helm render test in Task 3 is the
   other half of the guard.)

**Acceptance:** test fails before Task 2's values default is finalized only if values
drift; primarily it pins the contract. Runs green under `just test`. The negative test
fails if the fail-closed parse is ever loosened.

**Guardrails:** `just lint`, `just type`, `just test` (focused:
`uv run pytest tests/security/authz/test_demo_oidc_claims.py -q`).

**Commit:** `test(authz): pin demo OIDC default claim set round-trips RBAC parser`

---

## Task 2 — Make the demo claim set a Helm value, default to full RBAC grant

**Where it fits:** D1 + D2 + D4 — the core change.

**Files:**
- `deploy/helm/kdive/values.yaml` — add `demo.oidc.claims` (under the existing
  `demo.oidc` block that already holds `image`).
- `deploy/helm/kdive/templates/demo/oidc.yaml` — render `JSON_CONFIG` from the value.

**Steps:**
1. In `values.yaml`, under `demo.oidc`, add:
   ```yaml
   demo:
     oidc:
       image: ghcr.io/navikt/mock-oauth2-server:3.0.3   # unchanged
       # Demo-only. The bundled issuer mints a valid kdive token for ANY caller; this
       # is the claim set every such token carries. The default grants full RBAC over
       # the seeded `demo` project (admin) plus all three platform roles so a stock
       # demo deploy can exercise the entire RBAC/authz surface. NEVER front a real
       # RBAC boundary with this issuer. `aud` is pinned to ["kdive"] by the template
       # and cannot be overridden here. To test a denial, narrow the grant, e.g.
       # `--set demo.oidc.claims.roles.demo=viewer` or drop platform_roles. If you
       # change the project name, run `kdive seed-demo --project <name>` so it has a
       # budget/quota row.
       claims:
         sub: kdive-demo
         projects: ["demo"]
         roles:
           demo: admin
         platform_roles: ["platform_admin", "platform_operator", "platform_auditor"]
   ```
2. In `templates/demo/oidc.yaml`, replace the hardcoded `JSON_CONFIG` value with a
   render that:
   - starts from `.Values.demo.oidc.claims` (a map),
   - forces `sub` to a non-empty default (`default "kdive-demo" .Values.demo.oidc.claims.sub`)
     and forces `aud` to `["kdive"]` **regardless of any override**,
   - serializes the merged claims with `toJson`,
   - embeds it under `tokenCallbacks[0].requestMappings[0].claims` with
     `interactiveLogin:false` and `issuerId:default` unchanged.

   Sketch (use a local dict so `aud`/`sub` win over the override; `merge` mutates its
   first arg so start from the override and overlay the pinned floor, or build the floor
   and `merge` the override under it — pick the order that makes the pinned keys win;
   verify with the render tests):
   ```yaml
   {{- $claims := merge (dict "sub" (default "kdive-demo" .Values.demo.oidc.claims.sub) "aud" (list "kdive")) (omit .Values.demo.oidc.claims "aud" "sub") -}}
   - name: JSON_CONFIG
     value: '{"interactiveLogin":false,"tokenCallbacks":[{"issuerId":"default","requestMappings":[{"requestParam":"grant_type","match":"*","claims":{{ $claims | toJson }}}]}]}'
   ```
   Confirm `aud` cannot be overridden: `omit … "aud" "sub"` strips operator-set `aud`/`sub`
   before merge, then the pinned floor supplies them. Render-test AC2/AC3 prove this.

**Acceptance:** `helm template kdive deploy/helm/kdive --set bundledBackends=true --set
demoAcknowledged=true` renders `"projects":["demo"]`, `"roles":{"demo":"admin"}`, all
three platform roles, and `"aud":["kdive"]`. `--set demo.oidc.claims.roles.demo=viewer`
renders `"roles":{"demo":"viewer"}` and still `"aud":["kdive"]`. `helm lint` clean.

**Guardrails:** `just test` (helm render suite), `just lint`, then verify by hand:
`helm template kdive deploy/helm/kdive --set bundledBackends=true --set demoAcknowledged=true | grep JSON_CONFIG`.

**Rollback/cleanup:** none beyond reverting the two files; no schema/state.

**Commit:** `feat(helm): make demo OIDC claim set a value with a full-RBAC default`

---

## Task 3 — Helm render tests for the new behavior

**Where it fits:** Acceptance criteria 1–3.

**Files:**
- `tests/helm/test_helm_render.py` — extend (do not weaken existing tests).

**Steps (TDD — write before/with Task 2, watch them fail then pass):**
1. `test_bundled_oidc_mints_role_claims`: render bundled; assert the JSON_CONFIG string
   contains `"projects":["demo"]`, `"roles":{"demo":"admin"}`, and each of
   `"platform_admin"`, `"platform_operator"`, `"platform_auditor"`. Assertions match
   stable JSON substrings (toJson sorts keys, so `{"demo":"admin"}` is stable).
2. `test_bundled_oidc_claims_value_is_wired`: render with
   `--set demo.oidc.claims.roles.demo=viewer`; assert `"roles":{"demo":"viewer"}` present
   and `"roles":{"demo":"admin"}` absent; assert `projects`/`platform_roles` defaults
   still present (documents the deep-merge); assert `"aud":["kdive"]` still present.
3. `test_bundled_oidc_aud_pin_survives_override`: render with
   `--set demo.oidc.claims.aud=nope`; assert `"aud":["kdive"]` present and `"nope"`
   absent (proves D4 non-overridable aud).
4. Leave `test_bundled_oidc_pins_audience_kdive` unchanged — it must stay green (AC2).

**Acceptance:** all four pass; existing helm tests unchanged and green.

**Guardrails:** `uv run pytest tests/helm/test_helm_render.py -q` then `just test`.

**Commit:** fold into Task 2's commit if small, else
`test(helm): render-assert demo OIDC role claims and pinned aud`.

---

## Task 4 — Docs: README + NOTES.txt framing (D3)

**Where it fits:** D3 — an operator must know the demo token is now fully privileged and
how to narrow it.

**Files:**
- `deploy/helm/kdive/README.md` — "Bundled backends (demo only)" section.
- `deploy/helm/kdive/templates/NOTES.txt` — the demo-token mint note.

**Steps:**
1. README: add a sentence that the bundled issuer's tokens now carry an `admin` grant on
   project `demo` plus all three platform roles (full RBAC); narrow via
   `--set demo.oidc.claims...`; if you rename the project, `seed-demo` it.
2. NOTES.txt: where it shows how to mint a demo token, note the token carries the
   demo-project admin + platform-admin grant.
3. Keep language plain and demo-only; reinforce the existing ClusterIP-only / "mints a
   valid token for any caller" warning rather than restating it.

**Acceptance:** `just docs-check` and `just check-mermaid` green; README/NOTES describe
only what the diff does.

**Guardrails:** `just docs-check`, `just check-mermaid`.

**Commit:** `docs(helm): note demo OIDC token now carries the demo RBAC grant`

---

## Task 5 — Full gate + branch review

1. Run `just ci` (lint, type, lock-check, lint-shell, lint-workflows, check-mermaid,
   docs-check, config-docs-check, config-guard, chart-version-check, test). All green.
2. Run the branch adversarial-review loop (`/challenge --base main`). Address findings.
3. Push, open PR (`Closes #369`), drive to checks-green + CLEAN/MERGEABLE. Do **not**
   merge (orchestrator merges).

## Out of scope / explicitly not done

- No change to `src/kdive/security/authz/` parser (chart-only).
- No change to the external-backend path (OIDC Deployment is `bundledBackends`-gated).
- No `interactiveLogin:true` / login-form change.
- No new chart version bump unless `chart-version-check` demands it (it tracks
  appVersion↔pyproject, which this change does not touch).
