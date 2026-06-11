"""The M2.3 milestone exit-criterion proof (issue #272; mirrors the M2.2 boundary test).

The load-bearing proof that ``doctor`` is a *correct* preflight: for each of the four M2
contract faults, seeding the fault makes ``doctor`` name the **exact** remediation, and a
check that *cannot run* reads as ``error`` (a distinct nonzero exit) rather than a confident
wrong ``fail``. The whole verdict is driven through the real path — the real check classes,
the real :class:`DiagnosticsService` aggregation, the operator-gated ``ops.diagnostics`` tool
(audited against disposable Postgres), and the real ``doctor`` rendering + exit-code mapping —
so the asserted fix strings and exit codes are the ones an operator's gate actually observes.
Only the leaf provider seam (the TLS probe outcome, the ACL probe verdict, the secret
resolver, the egress probe guest) carries the seeded fault, exactly as the M1.5 fault-inject
mock provider seeds the seedable faults in CI.

Why this is not tautological: every check runs its production ``run()`` over a *broken input*
(an ``INVALID`` TLS outcome, a ``False`` ACL verdict, a resolver that raises, a ``BLOCKED``
egress guest), not a hand-built ``CheckResult``. The fix strings are imported from / asserted
against the constants and the check classes themselves (no prose is hardcoded in the proof),
so the proof cannot drift from the implementation. The verdict's three-state aggregation and
the ``has_failure``/``has_error`` flags flow from the real service and tool, and the exit code
is the real ``doctor`` mapping (``fail``→1, ``error``-with-no-``fail``→6, all-``pass``→0).

CI tier (this file): the three read checks plus the egress check are seeded with fakes through
the real chain — runnable in normal CI against the disposable-Postgres fixture (the egress
marker registry is DB-backed). The real-guest egress proof against a remote stack is the
operator-run band-gate evidence recorded in ``docs/runbooks/doctor-exit-criterion.md``; it is
deliberately NOT a CI check (no managed remote probe image exists until M2.4).
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.cli.commands import doctor
from kdive.diagnostics.checks import (
    Check,
    GdbstubAclCheck,
    GdbstubAclProbe,
    ProviderTlsCheck,
    SecretRefCheck,
    TlsProbe,
    TlsProbeOutcome,
)
from kdive.diagnostics.egress_probe import (
    EGRESS_FIX,
    EgressOutcome,
    EgressProbeRegistry,
    GuestEgressCheck,
    ProbeGuest,
    SingleFlight,
)
from kdive.diagnostics.service import DiagnosticsService
from kdive.health import HealthProbe
from kdive.health.server_checks import build_server_checks
from kdive.health.worker_checks import build_worker_checks
from kdive.mcp.tools.ops import diagnostics
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

# Fault-seeding fixtures (the "broken input" each real check runs over). Mirror the unit-test
# fixtures so the seeded fault matches the contract the M2 deployment actually broke.
_PROVIDER = "remote-libvirt"
_CA_PATH = "/etc/kdive/ca.pem"
_GDB_HOST = "10.0.0.5"
_PORT_RANGE = "47000-47099"
_PLATFORM_REF = "platform/oidc-secret"
_PROJECT_REF = "project/acme/db-password"

_FAIL_EXIT = 1
_ERROR_EXIT = 6
_PER_CHECK_TIMEOUT = 5.0


# ---- fault-seeding leaf seams (the only fixtures; everything above them is real) ----


def _tls_probe(outcome: TlsProbeOutcome) -> TlsProbe:
    async def _probe(ca_path: str) -> TlsProbeOutcome:
        return outcome

    return _probe


def _acl_probe(*, admitted: bool | None) -> GdbstubAclProbe:
    async def _probe(host: str, port_range: str) -> bool | None:
        return admitted

    return _probe


def _refs() -> list[tuple[str, bool]]:
    return [(_PLATFORM_REF, True), (_PROJECT_REF, False)]


def _missing_secret_resolver(missing: str) -> Callable[[str], None]:
    def _resolve(ref: str) -> None:
        if ref == missing:
            raise FileNotFoundError(ref)

    return _resolve


class _SeededGuest(ProbeGuest):
    """A probe guest scripted to a fixed egress outcome (the seeded egress fault)."""

    def __init__(self, outcome: EgressOutcome) -> None:
        self._outcome = outcome

    async def provision(self, domain_name: str) -> None:
        return None

    async def exec_egress(self, domain_name: str, presigned_url: str) -> EgressOutcome:
        return self._outcome

    async def teardown(self, domain_name: str) -> None:
        return None


async def _presigned() -> str:
    return "https://minio.local/bucket/probe?X-Amz-Credential=AKIA&X-Amz-Signature=deadbeef"


# ---- the real chain: service -> ops.diagnostics tool -> doctor exit code ------------


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _fixed_factory(service: DiagnosticsService) -> diagnostics.ServiceFactory:
    """A ``ServiceFactory`` that returns ``service`` regardless of provider / egress opt-in.

    The exit-criterion proof seeds the checks itself, so the factory is a constant; its
    signature matches the production protocol exactly (the operator-served path calls it
    through the real tool, not a shortcut).
    """

    def _build(provider: str | None, *, with_egress: bool = False) -> DiagnosticsService:
        return service

    return _build


def _operator_ctx() -> RequestContext:
    return RequestContext(
        principal="op-1",
        agent_session="sess-1",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
        client_id="kdivectl",
    )


def _doctor_exit_from_envelope(envelope: Mapping[str, object]) -> int:
    fields = doctor._envelope_fields(envelope)  # noqa: SLF001 - drive the real exit mapping
    return doctor._exit_code(fields)  # noqa: SLF001


def _rows_from_envelope(envelope: Mapping[str, object]) -> list[dict[str, object]]:
    fields = doctor._envelope_fields(envelope)  # noqa: SLF001 - drive the real row projection
    return doctor._rows(fields)  # noqa: SLF001


async def _serve_verdict(
    pool: AsyncConnectionPool, checks: Sequence[Check]
) -> tuple[list[dict[str, object]], int]:
    """Return (rendered rows, doctor exit code) for ``checks`` over the real chain.

    Drives the production verdict path end to end: the real :class:`DiagnosticsService`
    aggregates the (really-run) ``checks``, the operator-gated ``ops.diagnostics`` tool
    produces the verdict envelope (audited against the disposable Postgres fixture), and the
    real ``doctor`` code projects the rows and maps the gate-safe exit code. Nothing here
    computes the fix string or the exit code — both come straight from the implementation, so
    the proof cannot drift from what an operator's gate actually observes.
    """
    service = DiagnosticsService(checks=list(checks), per_check_timeout=_PER_CHECK_TIMEOUT)
    envelope = (
        await diagnostics.run_diagnostics(pool, _fixed_factory(service), _operator_ctx())
    ).model_dump()
    return _rows_from_envelope(envelope), _doctor_exit_from_envelope(envelope)


def _row_for(rows: list[dict[str, object]], check_id: str) -> dict[str, object]:
    matches = [r for r in rows if r["check"] == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id} row, got {len(matches)}"
    return matches[0]


# ---- exit-criterion 1: each seeded fault is flagged with the EXACT fix --------------


def test_seeded_tls_fault_flags_provider_tls_with_exact_fix(migrated_url: str) -> None:
    async def _run() -> None:
        check = ProviderTlsCheck(
            provider=_PROVIDER, ca_path=_CA_PATH, probe=_tls_probe(TlsProbeOutcome.INVALID)
        )
        # The exact fix the production check emits — derived from the check, not retyped here,
        # so the proof cannot drift from the implementation.
        expected_fix = (await check.run()).fix
        assert expected_fix is not None
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "provider_tls")
        assert row["status"] == "fail"
        assert row["fix"] == expected_fix
        assert "KDIVE_PROVIDER_CA" in str(row["fix"])
        assert code == _FAIL_EXIT

    asyncio.run(_run())


def test_seeded_gdbstub_acl_fault_flags_with_exact_fix(migrated_url: str) -> None:
    async def _run() -> None:
        check = GdbstubAclCheck(
            provider=_PROVIDER,
            host=_GDB_HOST,
            port_range=_PORT_RANGE,
            probe=_acl_probe(admitted=False),
        )
        expected_fix = (await check.run()).fix
        assert expected_fix is not None
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "gdbstub_acl")
        assert row["status"] == "fail"
        assert row["fix"] == expected_fix
        assert "open the host firewall / ACL for it" in str(row["fix"])
        assert code == _FAIL_EXIT

    asyncio.run(_run())


def test_seeded_missing_secret_ref_flags_with_exact_fix(migrated_url: str) -> None:
    async def _run() -> None:
        check = SecretRefCheck(refs=_refs(), resolve=_missing_secret_resolver(_PLATFORM_REF))
        expected_fix = (await check.run()).fix
        assert expected_fix is not None
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "secret_ref")
        assert row["status"] == "fail"
        assert row["fix"] == expected_fix
        assert "KDIVE_SECRETS_ROOT" in str(row["fix"])
        assert code == _FAIL_EXIT

    asyncio.run(_run())


def test_seeded_missing_secret_ref_never_discloses_a_per_tenant_ref(migrated_url: str) -> None:
    # The fault is a missing *project* ref: it must be counted (so the fail fires) but its
    # identifier must never reach the verdict — the diagnostic catches the fault without
    # becoming a cross-tenant secret-presence disclosure.
    async def _run() -> None:
        check = SecretRefCheck(refs=_refs(), resolve=_missing_secret_resolver(_PROJECT_REF))
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "secret_ref")
        assert row["status"] == "fail"
        assert _PROJECT_REF not in str(row["detail"])
        assert "acme" not in str(row["detail"])
        assert _PROJECT_REF not in str(row["fix"])
        assert code == _FAIL_EXIT

    asyncio.run(_run())


def test_seeded_egress_block_flags_guest_egress_with_the_forward_fix(migrated_url: str) -> None:
    # CI tier: the egress check runs its real run() over a probe guest seeded BLOCKED (the
    # in-guest exec saw the FORWARD DROP). The real DB-backed marker registry runs against the
    # disposable Postgres fixture. The real-guest remote proof is the operator-run band-gate
    # evidence in the runbook, not this CI check.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            check = GuestEgressCheck(
                provider=_PROVIDER,
                guest=_SeededGuest(EgressOutcome.BLOCKED),
                presigned_url=_presigned,
                registry=EgressProbeRegistry(pool),
                single_flight=SingleFlight(),
            )
            expected_fix = (await check.run()).fix
            assert expected_fix == EGRESS_FIX
            # Re-run through the real chain (the marker row from the verdict-run released).
            check = GuestEgressCheck(
                provider=_PROVIDER,
                guest=_SeededGuest(EgressOutcome.BLOCKED),
                presigned_url=_presigned,
                registry=EgressProbeRegistry(pool),
                single_flight=SingleFlight(),
            )
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "guest_egress")
        assert row["status"] == "fail"
        assert row["fix"] == EGRESS_FIX
        assert "FORWARD" in str(row["fix"])
        assert code == _FAIL_EXIT

    asyncio.run(_run())


def test_all_four_seeded_faults_in_one_run_each_named_with_its_exact_fix(
    migrated_url: str,
) -> None:
    # The headline exit criterion: a single doctor run over all four seeded faults names each
    # check's exact fix, and the gate exits on a fail (1). Asserting them together proves the
    # aggregating verdict keeps every check's distinct remediation (no fix bleeds across rows).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            checks: list[Check] = [
                SecretRefCheck(refs=_refs(), resolve=_missing_secret_resolver(_PLATFORM_REF)),
                ProviderTlsCheck(
                    provider=_PROVIDER, ca_path=_CA_PATH, probe=_tls_probe(TlsProbeOutcome.INVALID)
                ),
                GdbstubAclCheck(
                    provider=_PROVIDER,
                    host=_GDB_HOST,
                    port_range=_PORT_RANGE,
                    probe=_acl_probe(admitted=False),
                ),
                GuestEgressCheck(
                    provider=_PROVIDER,
                    guest=_SeededGuest(EgressOutcome.BLOCKED),
                    presigned_url=_presigned,
                    registry=EgressProbeRegistry(pool),
                    single_flight=SingleFlight(),
                ),
            ]
            expected = {c.id: (await _run_one(c)) for c in checks}  # exact fixes from the impl
            rows, code = await _serve_verdict(pool, checks)
        by_id = {r["check"]: r for r in rows}
        assert set(by_id) == {"secret_ref", "provider_tls", "gdbstub_acl", "guest_egress"}
        for check_id, expected_fix in expected.items():
            assert by_id[check_id]["status"] == "fail"
            assert by_id[check_id]["fix"] == expected_fix
            assert expected_fix  # every fault names a non-empty remediation
        assert code == _FAIL_EXIT

    asyncio.run(_run())


async def _run_one(check: Check) -> str | None:
    return (await check.run()).fix


# ---- exit-criterion 2: a check that cannot run is ERROR (exit 6), not FAIL (exit 1) -


def test_unreachable_provider_is_error_not_fail_and_exits_distinctly(migrated_url: str) -> None:
    # A provider/host that is simply down (the TLS chain may be fine) is the canonical
    # check-cannot-run case: it must read as error, never as a contract fail with a confident
    # wrong fix, and the gate must exit 6 (distinct from the 1 a real fail produces) so a gate
    # never goes green on a check that could not run.
    async def _run() -> None:
        check = ProviderTlsCheck(
            provider=_PROVIDER, ca_path=_CA_PATH, probe=_tls_probe(TlsProbeOutcome.UNREACHABLE)
        )
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(pool, [check])
        row = _row_for(rows, "provider_tls")
        assert row["status"] == "error"
        assert row["fix"] is None  # an error never carries a fix
        assert code == _ERROR_EXIT
        assert _ERROR_EXIT != _FAIL_EXIT

    asyncio.run(_run())


def test_fail_dominates_a_co_occurring_error(migrated_url: str) -> None:
    # A real contract fail must never be masked by an unrelated down dependency: a run with
    # both a seeded fail (blocked ACL) and a seeded error (unreachable TLS host) exits 1, not 6.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(
                pool,
                [
                    GdbstubAclCheck(
                        provider=_PROVIDER,
                        host=_GDB_HOST,
                        port_range=_PORT_RANGE,
                        probe=_acl_probe(admitted=False),
                    ),
                    ProviderTlsCheck(
                        provider=_PROVIDER,
                        ca_path=_CA_PATH,
                        probe=_tls_probe(TlsProbeOutcome.UNREACHABLE),
                    ),
                ],
            )
        assert _row_for(rows, "gdbstub_acl")["status"] == "fail"
        assert _row_for(rows, "provider_tls")["status"] == "error"
        assert code == _FAIL_EXIT  # fail dominates the co-occurring error

    asyncio.run(_run())


def test_healthy_run_exits_zero(migrated_url: str) -> None:
    # The other end of the gate: when every seeded input is healthy, the gate exits 0 — so a
    # nonzero exit is a real signal, not a constant.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rows, code = await _serve_verdict(
                pool,
                [
                    ProviderTlsCheck(
                        provider=_PROVIDER,
                        ca_path=_CA_PATH,
                        probe=_tls_probe(TlsProbeOutcome.VALID),
                    ),
                    GdbstubAclCheck(
                        provider=_PROVIDER,
                        host=_GDB_HOST,
                        port_range=_PORT_RANGE,
                        probe=_acl_probe(admitted=True),
                    ),
                    SecretRefCheck(refs=_refs(), resolve=lambda ref: None),
                ],
            )
        assert all(r["status"] == "pass" for r in rows)
        assert code == 0

    asyncio.run(_run())


# ---- exit-criterion 3: /readyz goes not-ready with a backend down on ALL THREE ------


def _server_probe(*, pg_ok: bool, minio_ok: bool, oidc_ok: bool) -> HealthProbe:
    return HealthProbe(
        checks=build_server_checks(
            postgres_ping=_async_gate(pg_ok),
            object_store_factory=lambda: _Store(ok=minio_ok),
            oidc_ping=_async_gate(oidc_ok),
        ),
        healthy_ttl=0.0,
    )


def _worker_probe(*, pg_ok: bool, minio_ok: bool) -> HealthProbe:
    return HealthProbe(
        checks=build_worker_checks(
            postgres_ping=_async_gate(pg_ok),
            object_store_factory=lambda: _Store(ok=minio_ok),
        ),
        healthy_ttl=0.0,
    )


class _Store:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok

    def ping(self) -> None:
        if not self._ok:
            raise RuntimeError("minio down")


def _async_gate(ok: bool) -> Callable[[], Awaitable[None]]:
    async def _probe() -> None:
        if not ok:
            raise RuntimeError("backend down")

    return _probe


def test_readyz_down_on_server_worker_and_reconciler() -> None:
    # The same dependency-set builders the three process entrypoints use (server adds OIDC;
    # worker and reconciler share the PG+MinIO set). Each process's probe flips not-ready when
    # one of its own backends is down, and is ready when all are up — proven on all three.
    async def _run() -> None:
        # server: PG down (still on its PG+MinIO+OIDC set) -> not ready.
        server_down = _server_probe(pg_ok=False, minio_ok=True, oidc_ok=True)
        server_result = await server_down.check()
        assert server_result.ready is False
        assert server_result.checks["postgres"] is False
        assert set(server_result.checks) == {"postgres", "minio", "oidc"}
        assert (await _server_probe(pg_ok=True, minio_ok=True, oidc_ok=True).check()).ready

        # worker: MinIO down (its PG+MinIO set, no OIDC) -> not ready.
        worker_result = await _worker_probe(pg_ok=True, minio_ok=False).check()
        assert worker_result.ready is False
        assert worker_result.checks["minio"] is False
        assert "oidc" not in worker_result.checks
        assert (await _worker_probe(pg_ok=True, minio_ok=True).check()).ready

        # reconciler: same builder as the worker (PG+MinIO, no OIDC); PG down -> not ready.
        reconciler_result = await _worker_probe(pg_ok=False, minio_ok=True).check()
        assert reconciler_result.ready is False
        assert reconciler_result.checks["postgres"] is False
        assert "oidc" not in reconciler_result.checks
        assert (await _worker_probe(pg_ok=True, minio_ok=True).check()).ready

    asyncio.run(_run())


# ---- guard: the operator gate is still enforced on the served verdict path ----------


def test_diagnostics_run_is_operator_gated_even_for_the_exit_criterion(migrated_url: str) -> None:
    # The exit-criterion proof drives the operator-served path; assert that path is the gated
    # one (a non-operator is denied with the authorization category), so the proof exercises
    # the real authz boundary rather than an ungated shortcut.
    async def _run() -> None:
        service = DiagnosticsService(checks=[_pass_check()], per_check_timeout=_PER_CHECK_TIMEOUT)
        async with _pool(migrated_url) as pool:
            ctx = RequestContext(
                principal="nobody",
                agent_session="sess-1",
                projects=(),
                roles={},
                platform_roles=frozenset(),
            )
            denied = await diagnostics.run_diagnostics(pool, _fixed_factory(service), ctx)
        assert denied.status == "error"
        assert denied.error_category == "authorization_denied"

    asyncio.run(_run())


def _pass_check() -> Check:
    return SecretRefCheck(refs=[], resolve=lambda ref: None)


def test_exit_criterion_test_runs_in_normal_ci() -> None:
    # A meta-assertion that this proof is CI-tier: it carries no live_stack/live_vm marker, so
    # it runs in normal CI (the seeded faults use fakes through the real chain). The remote
    # real-guest egress proof is operator-run band-gate evidence, recorded in the runbook.
    lines = pathlib.Path(__file__).read_text(encoding="utf-8").splitlines()
    decorators = [line.strip() for line in lines if line.lstrip().startswith("@")]
    assert not any("live_stack" in d or "live_vm" in d for d in decorators)
