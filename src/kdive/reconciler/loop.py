"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs the repairs — allocation
expiry, orphaned System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain,
idempotency-key GC, and (when an image store is wired) the three image-catalog sweeps:
leaked image objects, dangling image rows, and expired private images — each on a fresh
pooled connection, each fencing its writes, each isolated so one failing repair does not
starve the others. The expiry sweep runs first so an allocation it reclaims orphans its
System in the same pass. Time predicates use Postgres ``now()`` (never a Python clock).
Provider reaper contracts live in :mod:`kdive.providers.reaping`; the Postgres-only repair
path can use ``NullReaper`` there when no provider contributes leaked-infra repair.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import IMAGE_PUBLISH_GRACE
from kdive.providers.build_host.reachability import BuildHostProber
from kdive.providers.reaping import (
    BuildVmReaper,
    DumpVolumeReaper,
    InfraReaper,
    NullBuildVmReaper,
    NullDumpVolumeReaper,
)
from kdive.providers.transport_reset import NullResetter, TransportResetter
from kdive.reconciler import allocations as allocation_repairs
from kdive.reconciler import build_hosts as build_host_repairs
from kdive.reconciler import debug_sessions as debug_session_repairs
from kdive.reconciler import gc as gc_repairs
from kdive.reconciler import jobs as job_repairs
from kdive.reconciler import systems as system_repairs
from kdive.reconciler.console_hosting import CollectorRegistry
from kdive.reconciler.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.images import (
    repair_leaked_images as _repair_leaked_images,
)
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
from kdive.reconciler.provider_reaping import repair_leaked_domains as _repair_leaked_domains
from kdive.reconciler.provider_reaping import (
    repair_leaked_probe_guests as _repair_leaked_probe_guests,
)
from kdive.reconciler.uploads import (
    UploadStore,
)
from kdive.reconciler.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from kdive.services.images.retention import (
    ImageSweepStore,
)
from kdive.services.images.retention import (
    repair_expired_private_images as _repair_expired_private_images,
)

if TYPE_CHECKING:
    from kdive.health import Heartbeat

_log = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX_WAIT = allocation_repairs.DEFAULT_QUEUE_MAX_WAIT
DEFAULT_IDEMPOTENCY_RETENTION = gc_repairs.DEFAULT_IDEMPOTENCY_RETENTION
DEFAULT_DUMP_VOLUME_GRACE = gc_repairs.DEFAULT_DUMP_VOLUME_GRACE

_expire_one = allocation_repairs._expire_one
_gc_idempotency_keys = gc_repairs.gc_idempotency_keys
_promote_pending = allocation_repairs.promote_pending
_reap_console_collectors = gc_repairs.reap_console_collectors
_reap_orphaned_dump_volumes = gc_repairs.reap_orphaned_dump_volumes
_reap_orphaned_active_allocations = allocation_repairs.reap_orphaned_active_allocations
_reap_queue_timeouts = allocation_repairs.reap_queue_timeouts
_reap_queue_timeouts_for = allocation_repairs.reap_queue_timeouts_for
_reclaim_build_host_leases = build_host_repairs.reclaim_orphan_build_host_leases
_reap_orphan_build_vms = build_host_repairs.reap_orphan_build_vms
_probe_build_host_reachability = build_host_repairs.probe_build_host_reachability
_repair_abandoned_jobs = job_repairs.repair_abandoned_jobs
_repair_dead_sessions = debug_session_repairs.repair_dead_sessions
_repair_orphaned_systems = system_repairs.repair_orphaned_systems
_sweep_expired_allocations = allocation_repairs.sweep_expired_allocations

__all__ = [
    "ReconcileConfig",
    "ReconcileReport",
    "Reconciler",
    "_expire_one",
    "_gc_idempotency_keys",
    "_probe_build_host_reachability",
    "_promote_pending",
    "_reap_orphaned_active_allocations",
    "_reap_console_collectors",
    "_reap_orphaned_dump_volumes",
    "_reap_queue_timeouts",
    "_reclaim_build_host_leases",
    "_repair_abandoned_jobs",
    "_repair_dead_sessions",
    "_repair_orphaned_systems",
    "_sweep_expired_allocations",
    "reconcile_once",
]

# The default transport resetter (ADR-0086): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_RESETTER: TransportResetter = NullResetter()

# The default dump-volume reaper (ADR-0094): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_DUMP_VOLUME_REAPER: DumpVolumeReaper = NullDumpVolumeReaper()

# The default build-VM reaper (ADR-0100): a module-level stateless singleton (see above).
_NULL_BUILD_VM_REAPER: BuildVmReaper = NullBuildVmReaper()

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)
# Fallback image publish-deadline grace when the config setting is unset (its declared
# default is the same 3600s). A pending image row (or an orphan object with no row) is
# protected from the leaked/dangling image sweeps until this window past pending_since/mtime.
DEFAULT_IMAGE_PUBLISH_GRACE = timedelta(seconds=3600)

type _RepairFn = Callable[[AsyncConnection], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class _RepairSpec:
    name: str
    repair: _RepairFn


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    expired_allocations: int
    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    idempotency_keys_gc_count: int
    failures: tuple[str, ...]
    abandoned_uploads: int = 0
    reaped_active_allocations: int = 0
    promoted_allocations: int = 0
    queue_timeouts: int = 0
    leaked_probe_guests: int = 0
    leaked_images: int = 0
    dangling_images: int = 0
    expired_private_images: int = 0
    console_collectors_reaped: int = 0
    reaped_dump_volumes: int = 0
    reaped_build_vms: int = 0
    reclaimed_build_host_leases: int = 0
    build_host_states_changed: int = 0


@dataclass(frozen=True, slots=True)
class ReconcileConfig:
    """Optional reconciler ports and timing values."""

    resetter: TransportResetter = _NULL_RESETTER
    dump_volume_reaper: DumpVolumeReaper = _NULL_DUMP_VOLUME_REAPER
    build_vm_reaper: BuildVmReaper = _NULL_BUILD_VM_REAPER
    build_host_prober: BuildHostProber | None = None
    upload_store: UploadStore | None = None
    image_store: ImageSweepStore | None = None
    console_registry: CollectorRegistry | None = None
    interval: timedelta = DEFAULT_INTERVAL
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER
    idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION
    queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT
    dump_volume_grace: timedelta = DEFAULT_DUMP_VOLUME_GRACE
    heartbeat: Heartbeat | None = None
    heartbeat_tick: timedelta = timedelta(seconds=1)
    telemetry: ReconcilerTelemetry | None = None


_DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


def _repair_plan(
    *,
    reaper: InfraReaper,
    config: ReconcileConfig,
    image_publish_grace: timedelta,
) -> tuple[_RepairSpec, ...]:
    repairs = [
        _RepairSpec("expired_allocations", _sweep_expired_allocations),
        # Release leaked `active` allocations whose System is terminal/absent (ADR-0105) BEFORE
        # the promotion sweep, so a host-cap slot this reaper frees is filled in the same pass.
        _RepairSpec("reaped_active_allocations", _reap_orphaned_active_allocations),
        _RepairSpec("promoted_allocations", _promote_pending),
        _RepairSpec("queue_timeouts", _reap_queue_timeouts_for(config.queue_max_wait)),
        _RepairSpec("orphaned_systems", _repair_orphaned_systems),
        _RepairSpec("abandoned_jobs", _repair_abandoned_jobs),
        # Reap leaked build VMs BEFORE reclaiming their lease, so a freed slot never coexists
        # with a still-running leaked VM (ADR-0100 §4.6 over-admission window).
        _RepairSpec(
            "reaped_build_vms",
            lambda conn: _reap_orphan_build_vms(conn, config.build_vm_reaper),
        ),
        _RepairSpec("reclaimed_build_host_leases", _reclaim_build_host_leases),
        _RepairSpec(
            "dead_sessions",
            lambda conn: _repair_dead_sessions(
                conn, config.debug_session_stale_after, config.resetter
            ),
        ),
        _RepairSpec("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper)),
        _RepairSpec("leaked_probe_guests", lambda conn: _repair_leaked_probe_guests(conn, reaper)),
        _RepairSpec(
            "idempotency_keys_gc_count",
            lambda conn: _gc_idempotency_keys(conn, config.idempotency_retention),
        ),
        _RepairSpec(
            "reaped_dump_volumes",
            lambda conn: _reap_orphaned_dump_volumes(
                conn, config.dump_volume_reaper, config.dump_volume_grace
            ),
        ),
    ]
    if config.build_host_prober is not None:
        build_host_prober = config.build_host_prober
        repairs.append(
            _RepairSpec(
                "build_host_states_changed",
                lambda conn: _probe_build_host_reachability(conn, build_host_prober),
            )
        )
    if config.upload_store is not None:
        upload_store = config.upload_store
        repairs.append(
            _RepairSpec(
                "abandoned_uploads",
                lambda conn: _repair_abandoned_uploads(conn, upload_store),
            )
        )
    if config.console_registry is not None:
        console_registry = config.console_registry
        repairs.append(
            _RepairSpec(
                "console_collectors_reaped",
                lambda conn: _reap_console_collectors(conn, console_registry),
            )
        )
    if config.image_store is not None:
        image_store = config.image_store
        repairs.extend(
            (
                _RepairSpec(
                    "leaked_images",
                    lambda conn: _repair_leaked_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "dangling_images",
                    lambda conn: _repair_dangling_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "expired_private_images",
                    lambda conn: _repair_expired_private_images(conn, image_store),
                ),
            )
        )
    return tuple(repairs)


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    config: ReconcileConfig = _DEFAULT_RECONCILE_CONFIG,
) -> ReconcileReport:
    """Run the repairs once, each isolated, each on a fresh pooled connection.

    A repair that raises is logged, its name recorded in ``failures``, and the pass
    continues — one repair never starves the others. Returns the partial counts.

    The ``→expired`` allocation sweep runs **first** so that the allocations it moves to
    ``expired`` are seen as orphaning their System by :func:`_repair_orphaned_systems` in
    the **same** pass (ADR-0036 §4). The **promotion sweep runs right after the expiry
    sweep** so a slot a lease just freed is filled in the same pass; the
    **queue_timeout reaper runs after the promotion sweep** so every aged request already had
    its placement chance this pass (ADR-0069). The idempotency-key GC runs last.

    Counts are **best-effort**: a repair that commits some work and then raises (e.g. a
    transient DB error in a later iteration) reports ``0`` for its category and appears
    in ``failures`` — the committed work stands but is not reflected in the count. The
    per-domain ``destroy`` in :func:`_repair_leaked_domains` is caught individually, so
    the irreversible case (a domain destroyed, then a later failure) keeps its count.
    """
    counts, failures = await _run_repair_plan(
        pool,
        _repair_plan(
            reaper=reaper,
            config=config,
            image_publish_grace=_image_publish_grace(),
        ),
    )

    return ReconcileReport(
        expired_allocations=counts["expired_allocations"],
        orphaned_systems=counts["orphaned_systems"],
        abandoned_jobs=counts["abandoned_jobs"],
        dead_sessions=counts["dead_sessions"],
        leaked_domains=counts["leaked_domains"],
        idempotency_keys_gc_count=counts["idempotency_keys_gc_count"],
        failures=tuple(failures),
        abandoned_uploads=counts["abandoned_uploads"],
        reaped_active_allocations=counts["reaped_active_allocations"],
        promoted_allocations=counts["promoted_allocations"],
        queue_timeouts=counts["queue_timeouts"],
        leaked_probe_guests=counts["leaked_probe_guests"],
        leaked_images=counts.get("leaked_images", 0),
        dangling_images=counts.get("dangling_images", 0),
        expired_private_images=counts.get("expired_private_images", 0),
        console_collectors_reaped=counts.get("console_collectors_reaped", 0),
        reaped_dump_volumes=counts.get("reaped_dump_volumes", 0),
        reaped_build_vms=counts.get("reaped_build_vms", 0),
        reclaimed_build_host_leases=counts["reclaimed_build_host_leases"],
        build_host_states_changed=counts.get("build_host_states_changed", 0),
    )


def _image_publish_grace() -> timedelta:
    """Resolve the image publish-deadline grace from config (default 3600s)."""
    seconds = config.get(IMAGE_PUBLISH_GRACE)
    if seconds is None:
        return DEFAULT_IMAGE_PUBLISH_GRACE
    return timedelta(seconds=seconds)


async def _run_repair_plan(
    pool: AsyncConnectionPool, repairs: tuple[_RepairSpec, ...]
) -> tuple[dict[str, int], list[str]]:
    counts = {spec.name: 0 for spec in repairs}
    counts.setdefault("abandoned_uploads", 0)
    failures: list[str] = []
    for spec in repairs:
        try:
            async with pool.connection() as conn:
                counts[spec.name] = await spec.repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", spec.name, exc_info=True)
            failures.append(spec.name)
    return counts, failures


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        config: ReconcileConfig = _DEFAULT_RECONCILE_CONFIG,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._config = config
        self._heartbeat_tick = config.heartbeat_tick.total_seconds()
        self._telemetry = config.telemetry or ReconcilerTelemetry.disabled()

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool,
            self._reaper,
            config=self._config,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once` every ``interval``, surviving a transient pass error.

        The ``/livez`` heartbeat is bumped by a **background ticker** at
        :attr:`_heartbeat_tick` cadence (ADR-0090 §5), *not* per pass — so a single slow
        pass (an over-interval idempotency GC or a large domain sweep) never makes the
        reconciler read not-live; liveness tracks the event loop, not a repair. A wedged
        event loop stops the ticker too and ``/livez`` goes stale. Each pass also opens a
        span and records its duration plus the reconcile-lag (the gap between the
        scheduled and actual start, which grows when a pass overruns its interval).

        ``reconcile_once`` already isolates each repair, so a raise here is a rare
        whole-pass failure (e.g. pool acquisition); it is logged and the loop continues
        — a durable reconciler must not die on one bad pass.
        """
        ticker = self._start_heartbeat_ticker(stop)
        try:
            await self._pass_loop(stop)
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._config.heartbeat is None:
            return None
        return asyncio.create_task(
            _tick_until_stop(self._config.heartbeat, stop, self._heartbeat_tick)
        )

    async def _pass_loop(self, stop: asyncio.Event) -> None:
        interval = self._config.interval.total_seconds()
        next_due = time.monotonic()
        while not stop.is_set():
            self._telemetry.observe_lag(time.monotonic() - next_due)
            with self._telemetry.pass_span() as span:
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                    span.set_outcome("error")
                    _log.exception("reconcile pass failed; continuing after %ss", interval)
            next_due = time.monotonic() + interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)


async def _tick_until_stop(heartbeat: Heartbeat, stop: asyncio.Event, interval: float) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the pass loop so a long-running pass never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck reconciler still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            break
        heartbeat.tick()
