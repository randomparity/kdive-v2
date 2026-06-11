"""The ephemeral-probe-guest ``guest_egress`` check (ADR-0091 §3, §4).

``doctor`` is a preflight that may run with zero workload guests, so the headline M2 fault
— a guest→object-store egress path silently dropped by an unrelated host ``FORWARD`` policy
— cannot be observed from the operator's network or the worker host (both may take a
different path and false-green). ``guest_egress`` therefore provisions a **tiny short-lived
guest on the target provider**, execs a presigned ``HEAD``/``PUT`` against object-store
**from inside the guest** (the exact hop the ``FORWARD DROP`` broke), and tears it down.

Because this is the one check that provisions **real, cost-bearing infrastructure**, three
guards apply, all implemented here:

* **Reaper-owned cleanup, not assumed.** Teardown is best-effort; the guest is registered in
  ``egress_probe_guests`` under a reaper-visible marker carrying an **active-run heartbeat**
  and a **hard TTL** before it boots, so ``reconciler/provider_reaping`` reaps a leak even if
  this process dies mid-check. The reaper honors the heartbeat (never reaps a live run).
* **Single-flight per provider.** Concurrent callers do not each spin a guest: an in-process
  per-provider :class:`SingleFlight` shares one in-flight result, backstopped by the
  DB-level one-live-row-per-provider unique index.
* **Opt-in.** Assembled into the service only on ``doctor --with-egress`` (see
  ``diagnostics.service``); its provisioning action is audited distinctly.

The result is three-state: a blocked guest→object-store path is ``fail`` with the
open-the-``FORWARD`` fix; a provider/host unreachable (the guest never booted, the exec
channel was down) is ``error``, never a contract ``fail`` — emitting "open the FORWARD" when
the provider was simply down is the worst failure a diagnostic can have (ADR-0091 §2).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Coroutine
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage

EGRESS_ID = "guest_egress"

EGRESS_FIX = (
    "guest bridge -> object-store blocked (likely host FORWARD DROP); "
    "allow the guest subnet -> MinIO"
)

# Probe-guest domain names carry this prefix so the reaper recognizes an orphaned probe even
# if its `egress_probe_guests` row was never written (the guest booted before registration
# committed). The prefix plus the per-call token is the owned, reaper-visible marker.
PROBE_DOMAIN_PREFIX = "kdive-egress-probe-"

# The TTL is a backstop sized well above the probe's max runtime, NOT a deadline competing
# with a slow boot — otherwise the reaper would destroy a healthy guest mid-check and turn a
# passing egress path into a spurious `error` (ADR-0091 §3).
DEFAULT_PROBE_TTL = timedelta(minutes=10)

# A probe whose heartbeat has not advanced within this window is treated as leaked (its owning
# doctor run stopped beating). Sized below the TTL so a stalled run is reaped promptly while a
# live run that beats on the check's cadence is never mistaken for a leak.
DEFAULT_PROBE_HEARTBEAT_STALE_AFTER = timedelta(minutes=2)

_PRESIGNED_QUERY_RE = re.compile(r"\?.*$")


def redact_presigned(url: str) -> str:
    """Strip the query string from a presigned URL before it is logged or persisted.

    A presigned URL's signature and credential live entirely in its query string
    (``X-Amz-Credential``, ``X-Amz-Signature``, ...), so dropping the query yields a safe
    ``scheme://host/key`` form that still identifies the object without carrying the bearer
    capability. The probe never logs or returns the live URL — only this redacted form.
    """
    return _PRESIGNED_QUERY_RE.sub("?<redacted>", url)


class EgressOutcome(StrEnum):
    """The three observable outcomes of the in-guest egress exec."""

    REACHABLE = "reachable"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"


# Mints a fresh presigned object-store URL for the probe to exec against (HEAD/PUT). Returns
# the live URL — the caller redacts it before any log/persist/return.
PresignedUrlSource = Callable[[], Awaitable[str]]


class ProbeGuest(Protocol):
    """The narrow provider seam the egress check provisions and execs through.

    A realized provider (local-libvirt reuses the fixture image; remote needs an
    operator-staged image until M2.4, ADR-0091) implements this; CI exercises it with a fake.
    ``provision`` boots a guest under ``domain_name`` and returns once it can exec;
    ``exec_egress`` runs the presigned request from inside the guest; ``teardown`` is
    best-effort (the reaper is the backstop).
    """

    async def provision(self, domain_name: str) -> None: ...
    async def exec_egress(self, domain_name: str, presigned_url: str) -> EgressOutcome: ...
    async def teardown(self, domain_name: str) -> None: ...


class EgressProbeRegistry:
    """The DB-backed reaper-visible marker: register, heartbeat, release a probe guest.

    Each probe is a row in ``egress_probe_guests`` carrying ``heartbeat_at`` (the active-run
    heartbeat) and ``ttl_deadline`` (the hard backstop). The partial unique index on
    ``provider`` (live rows only) is the DB-level single-flight fence; this registry's
    ``release`` stamps ``released_at`` so the slot frees for the next run.
    """

    def __init__(self, pool: AsyncConnectionPool, *, ttl: timedelta = DEFAULT_PROBE_TTL) -> None:
        self._pool = pool
        self._ttl = ttl

    async def register(self, provider: str, domain_name: str) -> UUID:
        """Insert a live marker row; return its id. Raises on a duplicate live provider row."""
        async with (
            self._pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "INSERT INTO egress_probe_guests (provider, domain_name, ttl_deadline) "
                "VALUES (%s, %s, now() + %s) RETURNING id",
                (provider, domain_name, self._ttl),
            )
            row = await cur.fetchone()
        if row is None:  # invariant: INSERT ... RETURNING always yields one row
            raise RuntimeError("INSERT into egress_probe_guests returned no row")
        return row["id"]

    async def heartbeat(self, probe_id: UUID) -> None:
        """Advance the active-run heartbeat so the reaper never mistakes a live run for a leak."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE egress_probe_guests SET heartbeat_at = now() WHERE id = %s", (probe_id,)
            )

    async def release(self, probe_id: UUID) -> None:
        """Stamp ``released_at`` so the provider's single-flight slot frees for the next run."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE egress_probe_guests SET released_at = now() "
                "WHERE id = %s AND released_at IS NULL",
                (probe_id,),
            )


class SingleFlight:
    """Per-key in-process single-flight: concurrent callers share one in-flight coroutine.

    A second ``run`` for a key whose probe is in flight awaits the first call's result instead
    of spawning a second guest. The DB unique index is the cross-process backstop; this is the
    common-case (one process, a CI loop or two operators) coalescer (ADR-0091 §3).
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[CheckResult]] = {}
        self._lock = asyncio.Lock()

    async def run(
        self, key: str, factory: Callable[[], Coroutine[Any, Any, CheckResult]]
    ) -> CheckResult:
        """Run ``factory`` for ``key`` once; concurrent callers attach to the in-flight task."""
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():
                    del self._inflight[key]


class GuestEgressCheck(Check):
    """Worker-vantage: a guest on the provider bridge can reach object-store (ADR-0091 §3).

    Provisions an ephemeral probe guest under a reaper-visible marker (heartbeat + TTL), execs
    a presigned request from inside it, and tears it down best-effort. ``BLOCKED`` is ``fail``
    with the open-the-``FORWARD`` fix; ``UNREACHABLE`` (the guest never booted / the channel
    was down) is ``error`` — never a contract ``fail``. The single-flight coalescer ensures
    concurrent callers spin exactly one guest per provider.
    """

    def __init__(
        self,
        *,
        provider: str,
        guest: ProbeGuest,
        presigned_url: PresignedUrlSource,
        registry: EgressProbeRegistry,
        single_flight: SingleFlight,
    ) -> None:
        self._provider = provider
        self._guest = guest
        self._presigned_url = presigned_url
        self._registry = registry
        self._single_flight = single_flight

    @property
    def id(self) -> str:
        return EGRESS_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        return await self._single_flight.run(self._provider, self._probe_once)

    async def _probe_once(self) -> CheckResult:
        domain_name = f"{PROBE_DOMAIN_PREFIX}{uuid4().hex}"
        try:
            probe_id = await self._registry.register(self._provider, domain_name)
        except Exception:  # noqa: BLE001 - a live row already exists (single-flight) or DB down
            return self._error("could not register the probe marker; cannot provision a guest")
        try:
            return await self._provision_exec_verdict(domain_name, probe_id)
        finally:
            await self._teardown(domain_name)
            await self._registry.release(probe_id)

    async def _provision_exec_verdict(self, domain_name: str, probe_id: UUID) -> CheckResult:
        try:
            await self._guest.provision(domain_name)
            await self._registry.heartbeat(probe_id)
            url = await self._presigned_url()
            outcome = await self._guest.exec_egress(domain_name, url)
        except Exception:  # noqa: BLE001 - provision/exec failure is an indeterminate run -> error
            return self._error("probe guest could not be provisioned or reached; egress unverified")
        return self._verdict(outcome)

    async def _teardown(self, domain_name: str) -> None:
        try:
            await self._guest.teardown(domain_name)
        except Exception:  # noqa: BLE001 - teardown is best-effort; the reaper is the backstop
            return

    def _verdict(self, outcome: EgressOutcome) -> CheckResult:
        if outcome is EgressOutcome.REACHABLE:
            return CheckResult(
                check_id=EGRESS_ID,
                status=CheckStatus.PASS,
                detail="probe guest reached object-store from the provider bridge",
                provider=self._provider,
            )
        if outcome is EgressOutcome.UNREACHABLE:
            return self._error("probe guest could not reach the exec channel; egress unverified")
        return CheckResult(
            check_id=EGRESS_ID,
            status=CheckStatus.FAIL,
            detail="probe guest could not reach object-store from the provider bridge",
            fix=EGRESS_FIX,
            provider=self._provider,
        )

    def _error(self, detail: str) -> CheckResult:
        return CheckResult(
            check_id=EGRESS_ID,
            status=CheckStatus.ERROR,
            detail=detail,
            provider=self._provider,
        )
