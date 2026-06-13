"""The reconciler's reachability probe for SSH build hosts (#359, ADR-0103).

The reconciler flips ``build_hosts.state`` ``ready`` ↔ ``unreachable`` so build-host
selection skips a dead SSH builder proactively (today an unreachable host fails its build
and the lease reclaim later frees the slot). This narrow port lets the reconciler probe a
host without importing the build plane — mirroring :mod:`kdive.providers.transport_reset`.

``SshBuildHostProber`` is the only implementation; it is wired unconditionally in the
reconciler (SSH build hosts are independent of the remote-libvirt provider). The repair
that drives it lives in :mod:`kdive.reconciler.build_hosts`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from kdive.db.build_hosts import BuildHost
from kdive.domain.errors import CategorizedError
from kdive.providers.build_host.ssh_transport import (
    SshBuildTransport,
    materialized_ssh_identity,
)
from kdive.security.secrets.secret_registry import SecretRegistry

__all__ = ["BuildHostProber", "SshBuildHostProber"]

_log = logging.getLogger(__name__)

# Subprocess timeout for the probe. Deliberately larger than ssh's own ConnectTimeout=10
# (baked into SshBuildTransport's options) so ssh's connect timeout is the binding failure
# signal and this is only a backstop against a wedged ssh process (ADR-0103 §3.2).
DEFAULT_PROBE_TIMEOUT_S = 15


@runtime_checkable
class BuildHostProber(Protocol):
    """Report whether a build host is reachable (``True``) for proactive health (ADR-0103)."""

    async def probe(self, host: BuildHost) -> bool: ...


class SshBuildHostProber:
    """Probe an SSH build host with a bare ``ssh <host> true`` (ADR-0103).

    Holds the long-lived reconciler :class:`SecretRegistry`. Each probe materializes the
    host's SSH identity under a **per-probe scope** and releases it afterward, so the
    process-lifetime registry (whose global scope is never evicted) does not grow on every
    pass. The blocking ssh is offloaded with :func:`asyncio.to_thread` so a probe never
    stalls the reconciler event loop.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        probe_timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
    ) -> None:
        self._secret_registry = secret_registry
        self._probe_timeout_s = probe_timeout_s

    async def probe(self, host: BuildHost) -> bool:
        """Return whether ``host`` is reachable over SSH; never raises (fail-closed)."""
        return await asyncio.to_thread(self._probe_sync, host)

    def _probe_sync(self, host: BuildHost) -> bool:
        if host.address is None or host.ssh_credential_ref is None:
            return False
        # A fresh, non-None scope per probe so release() actually evicts the registration
        # (release is a documented no-op for the global scope=None).
        scope = object()
        try:
            with materialized_ssh_identity(
                host.ssh_credential_ref, self._secret_registry, scope=scope
            ) as identity_path:
                transport = SshBuildTransport(
                    address=host.address,
                    identity_path=identity_path,
                    secret_registry=self._secret_registry,
                )
                return transport.check_reachable(timeout_s=self._probe_timeout_s)
        except CategorizedError:
            # Credential resolve / identity materialization failure: treat as unreachable
            # (a host we cannot reach a credential for cannot build) rather than raising.
            _log.warning(
                "ssh build host %r could not be probed (credential/identity error); "
                "treating as unreachable",
                host.name,
            )
            return False
        finally:
            self._secret_registry.release(scope)
