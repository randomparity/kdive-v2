# Safety and RBAC

## Roles

KDIVE uses three project-scoped RBAC roles asserted by the identity provider
([ADR-0020](../adr/0020-rbac-audit-gate-implementation.md)):

| Role | Capabilities |
|---|---|
| `viewer` | Read access: `*.get`, `*.list`, read-only debug ops |
| `operator` | All viewer capabilities plus mutations: create allocations and runs, boot systems, attach debug sessions, perform reversible power ops |
| `admin` | All operator capabilities plus destructive ops (see below) |

In addition, a **platform tier** (`platform_admin`, `platform_operator`,
`platform_auditor`) provides cross-project authority for shared infrastructure
management. The platform tier is orthogonal to per-project roles.

Authorization failures raise before any tool response is built. The M0 taxonomy
maps a denial to `error_category: authorization_denied` on the wire.

## Destructive operations

Destructive operations are protected at two tiers
([ADR-0020](../adr/0020-rbac-audit-gate-implementation.md),
[ADR-0028](../adr/0028-control-plane-power-force-crash.md)).

### The three-factor gate

`control.force_crash` and `systems.reprovision` pass through the full
`assert_destructive_allowed` gate, which evaluates three independent checks that
must all pass (deny-by-default):

1. **Capability scope** — the operation kind is listed in the allocation's granted
   `capability_scope.destructive_ops`.
2. **RBAC role** — `force_crash` requires `admin`; `reprovision` requires
   `operator` (reprovisioning your own granted System is iterating, not
   administering).
3. **Provisioning-profile opt-in** — the controlling provisioning profile
   explicitly opts in to the operation (e.g. `destructive_ops: ["force_crash"]`).
   The default is an empty list; an unmodified profile cannot force-crash.

All three checks are evaluated and any missing check is reported. A denied
attempt is audited with `transition="<op>:denied"`, so a refusal leaves a trail.

### Admin-only destructive administration

Other destructive-administration ops — `control.power` (`off`/`cycle`/`reset`) and
`systems.teardown` — are **not** routed through the three-factor gate. They enforce
a single direct `require_role(..., admin)` check: no capability-scope or
profile-opt-in factor applies. The reversible `control.power on` requires only
`operator`.

## Secrets by reference

Cloud credentials, BMC/IPMI passwords, SSH keys, and HMC tokens never appear in
requests, state rows, or responses. The service stores only a reference
(`(present, source-ref)`). The worker resolves the reference from a pluggable
secret backend at the worker boundary — **and registers the resolved value into
the redaction registry before the value is handed to any subprocess or transport**
([ADR-0027](../adr/0027-safety-modules-secret-backend-impl.md)). This ordering is
structural: the `FileRefBackend` registers before returning, so a caller cannot
receive the value without it already being in the registry.

## Mandatory redaction

All guest output, gdb/SoL transcripts, and console logs pass through the
`Redactor` before persistence and before any response snippet. The redactor masks
known secret values by exact-value replacement and `key=value` pairs whose key
matches the secret-name pattern. The `ToolResponse` envelope has no field for
inline log text — artifact bytes are accessed only via `artifacts.get` after the
agent inspects the `refs` reference. Raw artifacts in the object store are marked
sensitive and are fetched only by explicit request.

Output produced before a secret is registered is quarantined in the object store
(marked sensitive) until it can be redacted.

## Audit log

Every state transition and every destructive op writes an append-only audit row
attributing `(principal, agent_session, tool, args-digest)`. The `args` are stored
only as a SHA-256 digest, not in the clear, so the log provides tamper-evidence
and correlation without persisting low-entropy secret material.
