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
        "src/kdive/mcp/app.py",
        # Server telemetry middleware (#266, ADR-0090 §5): a provider-agnostic platform
        # change adding TelemetryMiddleware (a span per MCP tool call + per-tool RED
        # metrics) at the dispatch boundary and registering it in build_app. The labels
        # are restricted to the tool name + outcome (no provider/tenant data); none of it
        # is provider-specific.
        "src/kdive/mcp/middleware.py",
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
