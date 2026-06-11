"""The `Check` framework and the three read-only diagnostic checks (ADR-0091 §2).

A `Check` is an `id`, a `vantage`, and an async `run() -> CheckResult`, where
`CheckResult.status` is **three-state**: `pass` (the contract holds), `fail` (the
contract is violated and `fix` names the exact remediation), and `error` (the check
could not be run to a verdict — the backend was down, the host was unreachable, the
probe timed out — and `detail` says what blocked it, *never* a contract-fix string).
Collapsing `error` into `fail` is the worst failure a diagnostic can have: it would emit
a confident wrong fix from the one tool whose value is naming the right one.

Every check runs through :func:`run_check`, which bounds it by a per-check timeout (a
check that does not answer is `error`, not a hang) and converts any unexpected
exception into `error` — so a check can never wedge or crash the aggregating service.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

SECRET_REF_ID = "secret_ref"
PROVIDER_TLS_ID = "provider_tls"
GDBSTUB_ACL_ID = "gdbstub_acl"


class CheckStatus(StrEnum):
    """The three-state verdict of a single check (ADR-0091 §2)."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class Vantage(StrEnum):
    """Where a check must run from to observe the contract it probes.

    ``kdivectl`` on an operator laptop cannot see the worker→hypervisor TLS chain, so a
    check declares its vantage and the deployment runs it from there (ADR-0091 §1).
    """

    SERVER = "server"
    WORKER = "worker"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's three-state verdict (ADR-0091 §2).

    Args:
        check_id: The stable id of the check that produced this result.
        status: The three-state verdict.
        detail: On ``fail``, what contract is violated; on ``error``, what *blocked* the
            check (never a fix string); on ``pass``, a short confirmation.
        fix: The exact remediation — mandatory on ``fail``, forbidden otherwise (an
            ``error``/``pass`` carrying a fix is a producer bug).
        provider: The provider this result pertains to, or ``None`` for a
            provider-independent check (``secret_ref``).
    """

    check_id: str
    status: CheckStatus
    detail: str
    fix: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        if self.status is CheckStatus.FAIL and not self.fix:
            raise ValueError(f"{self.check_id}: a fail result must name a fix")
        if self.status is not CheckStatus.FAIL and self.fix is not None:
            raise ValueError(
                f"{self.check_id}: only a fail result may carry a fix "
                f"(status {self.status.value!r} carried {self.fix!r})"
            )


class Check(ABC):
    """A single diagnostic probe with an explicit vantage and a three-state verdict."""

    @property
    @abstractmethod
    def id(self) -> str:
        """The stable check id (e.g. ``secret_ref``)."""

    @property
    @abstractmethod
    def vantage(self) -> Vantage:
        """Where this check must run from."""

    @abstractmethod
    async def run(self) -> CheckResult:
        """Probe the contract and return a three-state result.

        Implementations return ``error`` for an indeterminate run rather than raising;
        :func:`run_check` is the backstop that maps a leaked exception or timeout to
        ``error`` so the aggregating service can never wedge.
        """


async def run_check(check: Check, *, timeout: float) -> CheckResult:
    """Run ``check`` bounded by ``timeout``; map a timeout or unexpected error to ``error``.

    A check that does not answer within ``timeout`` is an ``error`` with a
    "did not respond within N" detail — never a hang and never a contract ``fail``. Any
    exception the check leaks is also mapped to ``error`` with a generic blocked-reason
    detail (the exception text is not surfaced, so an unexpected backend message cannot
    leak through the verdict).
    """
    try:
        async with asyncio.timeout(timeout):
            return await check.run()
    except TimeoutError:
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail=f"check did not respond within {timeout:g}s",
        )
    except Exception:  # noqa: BLE001 - backstop: a leaked error must not wedge the service
        return CheckResult(
            check_id=check.id,
            status=CheckStatus.ERROR,
            detail="check could not be run to a verdict (unexpected error)",
        )


# A resolver raises on an unresolved ref; the secret backend's own unreachable-exception
# type (passed separately) is the error-vs-fail discriminator.
SecretResolve = Callable[[str], object]


class SecretRefCheck(Check):
    """Server-vantage: every configured secret ref resolves in the backend (ADR-0091 §2).

    Full coverage spans both platform and per-tenant refs (the motivating M2 fault did not
    assume which kind). Non-disclosure is enforced on the **reporting** surface: the verdict
    reports aggregate pass/fail counts and platform-ref detail only — a per-tenant ref that
    fails to resolve is counted but its identifier is never surfaced, so the diagnostic
    catches every unresolved ref without becoming a cross-tenant secret-presence disclosure.

    A backend that cannot be reached at all (``backend_unreachable`` raised) is ``error``,
    not a contract ``fail`` — the refs may all be fine.
    """

    def __init__(
        self,
        *,
        refs: Sequence[tuple[str, bool]],
        resolve: SecretResolve,
        backend_unreachable: type[Exception] | tuple[type[Exception], ...] = (),
    ) -> None:
        """Build the check.

        Args:
            refs: ``(ref, is_platform)`` pairs for every configured secret ref. The
                ``is_platform`` flag gates whether the ref identifier may appear in
                ``detail`` (platform refs are operator-owned config, not tenant data).
            resolve: Resolves one ref, raising on a ref that does not resolve.
            backend_unreachable: Exception type(s) signalling the backend itself is
                unreachable (→ ``error``), distinct from a per-ref miss (→ ``fail``).
        """
        self._refs = list(refs)
        self._resolve = resolve
        self._unreachable = backend_unreachable

    @property
    def id(self) -> str:
        return SECRET_REF_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        unresolved_platform: list[str] = []
        unresolved_count = 0
        try:
            for ref, is_platform in self._refs:
                if not self._resolves(ref):
                    unresolved_count += 1
                    if is_platform:
                        unresolved_platform.append(ref)
        except self._unreachable_types():
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="secret backend unreachable; cannot verify any ref",
            )
        return self._verdict(unresolved_count, unresolved_platform)

    def _resolves(self, ref: str) -> bool:
        try:
            self._resolve(ref)
        except self._unreachable_types():
            raise
        except Exception:  # noqa: BLE001 - any per-ref resolution failure is an unresolved ref
            return False
        return True

    def _unreachable_types(self) -> tuple[type[Exception], ...]:
        if isinstance(self._unreachable, tuple):
            return self._unreachable
        return (self._unreachable,)

    def _verdict(self, unresolved: int, unresolved_platform: list[str]) -> CheckResult:
        total = len(self._refs)
        if unresolved == 0:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"all {total} configured secret refs resolve",
            )
        platform_detail = (
            f" (unresolved platform refs: {', '.join(sorted(unresolved_platform))})"
            if unresolved_platform
            else ""
        )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"{unresolved} of {total} configured secret refs do not resolve"
            + platform_detail,
            fix=(
                "secret ref does not resolve under KDIVE_SECRETS_ROOT; "
                "create the file-ref or fix the path"
            ),
        )


class TlsProbeOutcome(StrEnum):
    """The three observable outcomes of a provider TLS probe."""

    VALID = "valid"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"


TlsProbe = Callable[[str], Awaitable[TlsProbeOutcome]]


class ProviderTlsCheck(Check):
    """Worker-vantage: the provider TLS chain validates against the configured CA.

    Host-unreachable is ``error`` (the chain may be fine; the host is simply down);
    cert-invalid is ``fail`` with the reissue/CA-path remediation (ADR-0091 §2).
    """

    def __init__(self, *, provider: str, ca_path: str, probe: TlsProbe) -> None:
        self._provider = provider
        self._ca_path = ca_path
        self._probe = probe

    @property
    def id(self) -> str:
        return PROVIDER_TLS_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        outcome = await self._probe(self._ca_path)
        if outcome is TlsProbeOutcome.VALID:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"provider TLS chain validates against {self._ca_path}",
                provider=self._provider,
            )
        if outcome is TlsProbeOutcome.UNREACHABLE:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail="provider host unreachable; cannot validate the TLS chain",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"provider cert not signed by configured CA {self._ca_path}",
            fix=(
                f"provider cert not signed by configured CA {self._ca_path}; "
                "reissue or set KDIVE_PROVIDER_CA"
            ),
            provider=self._provider,
        )


# Returns True if the ACL admits the range, False if blocked, None if indeterminate.
GdbstubAclProbe = Callable[[str, str], Awaitable[bool | None]]


class GdbstubAclCheck(Check):
    """Worker-vantage: the host ACL on ``config.gdb_addr`` admits the gdbstub port range.

    A **policy** check, not a live-port check: the gdbstub port is assigned per-domain
    (ADR-0083), so a cold preflight with zero running guests has no concrete port —
    validating that the ACL admits the configured range needs no live domain and catches
    the M2 fault (a closed ACL) directly. An indeterminate probe (``None``) is ``error``.
    """

    def __init__(
        self, *, provider: str, host: str, port_range: str, probe: GdbstubAclProbe
    ) -> None:
        self._provider = provider
        self._host = host
        self._port_range = port_range
        self._probe = probe

    @property
    def id(self) -> str:
        return GDBSTUB_ACL_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        admitted = await self._probe(self._host, self._port_range)
        if admitted is None:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail=f"could not determine the ACL on {self._host} for {self._port_range}",
                provider=self._provider,
            )
        if admitted:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail=f"ACL on {self._host} admits gdbstub range {self._port_range}",
                provider=self._provider,
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.FAIL,
            detail=f"gdbstub port range {self._port_range} on {self._host} blocked",
            fix=(
                f"gdbstub port range {self._port_range} on {self._host} blocked; "
                "open the host firewall / ACL for it"
            ),
            provider=self._provider,
        )
