"""Per-PR M2 portability gate (ADR-0076).

Measures the cumulative touched lines (per-commit added+removed — not a net a later
revert can zero out) of every commit since the ``pre-M2`` tag over the
provider-agnostic core (domain/db/jobs/reconciler/services/store/security and the
whole ``mcp`` package including ``mcp/tools/*``), and fails when any file outside the
named allowlist is touched. A second, net ``git diff`` check covers the per-commit
walk's blind spot: a core change introduced only in a merge commit (a conflict
resolution or evil merge), which ``--no-merges`` numstat never sees. The allowlist is
the ADR-0076 set: the ``ResourceKind`` enum value, the one M2 migration, and the
additive ``presign_get`` primitive. Extending it is a deliberate, reviewed decision —
edit this file in the same PR.

Exit codes: 0 gate passes; 1 violations found; 2 the baseline tag is unavailable.
Stdlib-only: CI runs it without a synced environment (``just m2-gate``).
"""

from __future__ import annotations

import subprocess
import sys

BASELINE_TAG = "pre-M2"

CORE_PREFIXES = (
    "src/kdive/domain/",
    "src/kdive/db/",
    "src/kdive/jobs/",
    "src/kdive/reconciler/",
    "src/kdive/services/",
    "src/kdive/store/",
    "src/kdive/security/",
    "src/kdive/mcp/",
)

ALLOWED_FILES = frozenset(
    {
        # ResourceKind.REMOTE_LIBVIRT (ADR-0076 named touch-point).
        "src/kdive/domain/models.py",
        # The one M2 migration: the resources.kind CHECK widen.
        "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
        # The additive presign_get primitive (ADR-0076, ADR-0078).
        "src/kdive/store/objectstore.py",
        # drgn-live transport generalization (#215, ADR-0085): the deliberate, reviewed core
        # touch routing remote in-guest drgn off the ssh-credential + ssh-string assumption.
        "src/kdive/mcp/tools/debug/sessions.py",
        "src/kdive/mcp/tools/debug/introspect.py",
        # Dead-worker gdbstub reconciler reset (#216, ADR-0086): the deliberate, reviewed core
        # touch that resets a stale session's transport through the injected TransportResetter
        # port so a dead worker's single-client gdbstub stops blocking re-attach.
        "src/kdive/reconciler/loop.py",
        # Central config-registry migration (#233, ADR-0087): a one-time, reviewed platform
        # refactor routing the scattered KDIVE_* reads in these agnostic-core modules through
        # kdive.config. This is shared-infra (platform) work, not provider work; kdive/config/
        # itself is outside CORE_PREFIXES, so only these in-place reader migrations register here.
        "src/kdive/db/pool.py",
        "src/kdive/domain/lease.py",
        "src/kdive/mcp/auth.py",
        "src/kdive/mcp/tools/catalog/artifacts_uploads.py",
        "src/kdive/mcp/tools/debug/ops.py",
        "src/kdive/security/secrets/secrets.py",
        # Operator-CLI audit attribution (#248, ADR-0089): the milestone's only non-cli core
        # change. A provider-agnostic addition — record the caller class (operator-cli | agent
        # | unknown) resolved from the OIDC client_id on every platform_audit_log row. The
        # required `actor` field threads through the shared audit chokepoints and every inline
        # success site; none of it is provider-specific.
        "src/kdive/db/schema/0021_platform_audit_actor.sql",
        "src/kdive/security/authz/actor.py",
        "src/kdive/security/authz/context.py",
        "src/kdive/security/audit.py",
        "src/kdive/mcp/tools/ops/_auth.py",
        "src/kdive/mcp/tools/ops/_reads.py",
        "src/kdive/mcp/tools/ops/breakglass.py",
        "src/kdive/mcp/tools/ops/queue.py",
        "src/kdive/mcp/tools/ops/reconcile.py",
        "src/kdive/mcp/tools/ops/resources.py",
        "src/kdive/mcp/tools/ops/tuning.py",
        "src/kdive/mcp/tools/accounting/reports.py",
        "src/kdive/mcp/tools/catalog/shapes.py",
        # M2.2 admin-CLI net-new read tools (#252, ADR-0089 §6): two provider-agnostic
        # platform reads on the agnostic core. secrets.list reports secret *presence* (the
        # scope_refs projection on SecretRegistry — never values), platform-operator gated;
        # fixtures.list is a plain authenticated rootfs-catalog read. Their app.py registrar
        # wiring and the value-free scope_refs accessor carry no provider-specific logic.
        "src/kdive/mcp/tools/ops/secrets.py",
        "src/kdive/mcp/tools/catalog/fixtures.py",
        "src/kdive/security/secrets/secret_registry.py",
        # M2.3 doctor diagnostics tool (#269, ADR-0091): a provider-agnostic platform-operator
        # tool that runs an assembled set of read-only Checks and aggregates one verdict. It holds
        # no provider-specific logic — the per-provider checks live in kdive/diagnostics/ (outside
        # CORE_PREFIXES) and reach the tool only through the injected service factory; this module
        # is the same authz-gated/audited ops surface as its siblings above.
        "src/kdive/mcp/tools/ops/diagnostics.py",
        "src/kdive/mcp/app.py",
        # Server telemetry middleware (#266, ADR-0090 §5): a provider-agnostic platform
        # change adding TelemetryMiddleware (a span per MCP tool call + per-tool RED
        # metrics) at the dispatch boundary and registering it in build_app. The labels
        # are restricted to the tool name + outcome (no provider/tenant data); none of it
        # is provider-specific.
        "src/kdive/mcp/middleware.py",
        # M2.3 ephemeral-probe-guest egress check (#270, ADR-0091 §3): the heartbeat-honoring
        # reaper sweep for leaked `guest_egress` probe guests and its marker table. Both are
        # provider-agnostic — the reconciler reaps a probe by domain name through its existing
        # InfraReaper (no provider-specific branch), and the table is the reaper-visible marker
        # (active-run heartbeat + hard TTL). The probe-guest provision/exec seam itself lives in
        # kdive/diagnostics/ (outside CORE_PREFIXES) and is provider-wired by the live gate.
        "src/kdive/reconciler/provider_reaping.py",
        "src/kdive/db/schema/0022_egress_probe_guests.sql",
        # Worker/reconciler telemetry + aux health gate (#267, ADR-0090 §5): a
        # provider-agnostic platform change. worker.py gains the loop-granularity /livez
        # heartbeat tick, the not-ready dequeue pause, and a per-job span; the two
        # *_telemetry modules build the per-job/per-pass spans + duration/queue-depth/lag
        # metrics over the facade providers, labelled only by job_kind/outcome (no
        # provider/tenant data). reconciler/loop.py (already allowlisted above) gains the
        # per-pass span + heartbeat tick. queue.py gains a read-only count_claimable used
        # by the queue-depth gauge. None of it is provider-specific.
        "src/kdive/jobs/worker.py",
        "src/kdive/jobs/worker_telemetry.py",
        "src/kdive/jobs/queue.py",
        "src/kdive/reconciler/loop_telemetry.py",
        # M2.4 image_catalog (#282, ADR-0092/0093): the DB-backed image catalog that replaces the
        # read-only YAML rootfs catalog as the single source of truth. A provider-agnostic
        # platform addition — the single M2.4 migration's full public+private schema, the
        # ImageCatalogEntry model + ImageVisibility/ImageState enums in models.py (already
        # allowlisted), and the IMAGE_CATALOG repository binding (a plain Repository over the new
        # table). No provider-specific logic; the provider materialize cutover lives outside
        # CORE_PREFIXES (providers/local_libvirt/...).
        "src/kdive/db/schema/0023_image_catalog.sql",
        "src/kdive/db/repositories.py",
        # M2.4 publish/register + IMAGE_BUILD job (#285, ADR-0092): the provider-agnostic
        # row-first publish/register two-write service, the IMAGE_BUILD job kind + handler
        # (build -> guest-contract-validate -> publish), the typed ImageBuildPayload, and the
        # jobs.kind CHECK widen that admits the new kind. No provider-specific logic — the
        # handler drives an injected RootfsBuildPlane and the publish service stores whatever
        # the PublishRequest carries; the concrete build plane lives under kdive/images/
        # (outside CORE_PREFIXES).
        "src/kdive/services/images/__init__.py",
        "src/kdive/services/images/publish.py",
        "src/kdive/jobs/handlers/image_build.py",
        "src/kdive/jobs/payloads.py",
        "src/kdive/db/schema/0024_image_build_job_kind.sql",
        # M2.4 private upload path (#286, ADR-0093): the provider-agnostic project-private
        # upload registration service. Under the project advisory lock it enforces the
        # per-project count/bytes quota fail-closed, validates the quarantined object's guest
        # contract, then delegates to the publish service with visibility='private'. No
        # provider-specific logic — it reuses the IMAGE_PRIVATE_* core settings and the existing
        # publish two-write; the new settings live in config/core_settings.py (already core).
        "src/kdive/services/images/upload.py",
        # M2.4 reconciler image sweeps (#287, ADR-0092/0093): three provider-agnostic,
        # deadline-guarded drift sweeps over the image_catalog + image-prefix objects (leaked
        # objects with no row, dangling rows whose object is gone, expired private images —
        # reference-guarded + extend-fenced). The sweeps consume the narrow ImageSweepStore port
        # (an ObjectStore satisfies it) and the catalog table; no provider-specific logic.
        # reconciler/loop.py (already allowlisted) appends the three _RepairSpecs + report counts.
        "src/kdive/reconciler/images.py",
    }
)


def parse_numstat(out: str) -> dict[str, int]:
    """Aggregate per-file touched lines (added+removed) from ``git log --numstat`` output.

    Binary files render as ``-\\t-\\tpath`` and count as one touched line. Only files
    under the core prefixes are the gate's subject.
    """
    touched: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path.startswith(CORE_PREFIXES):
            continue
        lines = 1 if added == "-" else int(added) + int(removed)
        touched[path] = touched.get(path, 0) + max(lines, 1)
    return touched


def violations(touched: dict[str, int]) -> dict[str, int]:
    """The non-allowlisted core files with any cumulative touch."""
    return {path: count for path, count in touched.items() if path not in ALLOWED_FILES}


def render_report(touched: dict[str, int]) -> str:
    """Render the measurement as a markdown report (pure function over the touched map).

    Used by ``--report`` to write the committed milestone-end record (``just m2-report``). The
    verdict mirrors the gate: a non-allowlisted core touch is a violation and fails.
    """
    allowed = {path: count for path, count in touched.items() if path in ALLOWED_FILES}
    bad = violations(touched)
    lines = [
        "# M2 portability report",
        "",
        f"Cumulative touched lines of the M2 commit set since the `{BASELINE_TAG}` tag, over the",
        "provider-agnostic core surface (ADR-0076). Generated by `just m2-report` — do not",
        "hand-edit.",
        "",
        "## Allowlisted touch-points",
        "",
        "| cumulative lines | file |",
        "|---:|---|",
    ]
    lines.extend(f"| {count} | `{path}` |" for path, count in sorted(allowed.items()))
    lines.append("")
    if bad:
        lines += [
            "## Violations",
            "",
            "| cumulative lines | file |",
            "|---:|---|",
            *(f"| {count} | `{path}` |" for path, count in sorted(bad.items())),
            "",
            "**Verdict: gate FAILED** — provider-specific changes reached the core surface.",
        ]
    else:
        lines.append(
            "**Verdict: gate passed** — no core surface touched outside the ADR-0076 allowlist."
        )
    # No trailing blank element: ``print`` adds the single final newline the
    # end-of-file hook enforces, so the committed report regenerates byte-identically.
    return "\n".join(lines)


def _measure() -> dict[str, int] | None:
    """Compute the cumulative touched map, or None if the baseline tag is unavailable."""
    tag_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"{BASELINE_TAG}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if tag_check.returncode != 0:
        print(
            f"error: baseline tag {BASELINE_TAG!r} is unavailable; fetch tags/history "
            "(CI: actions/checkout with fetch-depth: 0)",
            file=sys.stderr,
        )
        return None
    log = subprocess.run(
        [
            "git",
            "log",
            "--numstat",
            "--no-merges",
            "--no-renames",
            "--format=",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    touched = parse_numstat(log.stdout)
    # Union in the net diff: it sees merge-commit-only changes the per-commit walk
    # misses, while the per-commit sum keeps reverted changes counted.
    net = subprocess.run(
        [
            "git",
            "diff",
            "--numstat",
            "--no-renames",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for path, count in parse_numstat(net.stdout).items():
        touched[path] = max(touched.get(path, 0), count)
    return touched


def main() -> int:
    touched = _measure()
    if touched is None:
        return 2
    if "--report" in sys.argv[1:]:
        print(render_report(touched))
        return 1 if violations(touched) else 0
    allowed = {path: count for path, count in touched.items() if path in ALLOWED_FILES}
    print(f"M2 portability measurement since {BASELINE_TAG} (cumulative touched lines):")
    for path, count in sorted(allowed.items()):
        print(f"  allowlisted  {count:>6}  {path}")
    bad = violations(touched)
    if bad:
        print("\ngate FAILED - provider-specific changes reached the core surface:")
        for path, count in sorted(bad.items()):
            print(f"  VIOLATION    {count:>6}  {path}")
        print(
            "\nRefactor the provider logic out of core (the M2 co-equal goal, "
            "docs/specs/m2-remote-libvirt.md), or - for a deliberate provider-agnostic "
            "core change - extend ALLOWED_FILES in this script in the same PR."
        )
        return 1
    print("gate passed: no core surface touched outside the ADR-0076 allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
