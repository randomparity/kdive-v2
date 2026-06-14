"""Dispatch a build onto the selected build-host transport."""

from __future__ import annotations

import asyncio
from uuid import UUID

from kdive.db.build_hosts import BuildHost, BuildHostKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitKernelSource, ServerBuildProfile
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.build_host.ssh_transport import SshBuildTransport
from kdive.providers.build_host.transport import BuildTransport
from kdive.providers.ports import Builder, TransportCapableBuilder
from kdive.providers.remote_libvirt.lifecycle.build_vm import ephemeral_build_session
from kdive.security.secrets.secret_registry import SecretRegistry

# Patchable seams: tests substitute these to avoid real SSH or build-VM provisioning.
ssh_build_transport_from_host = SshBuildTransport.from_host
ephemeral_build_transport_from_host = ephemeral_build_session


async def run_build_on_host(
    builder: Builder,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    *,
    secret_registry: SecretRegistry,
) -> BuildOutput:
    """Run ``builder`` on the selected build host."""
    if host.kind is BuildHostKind.LOCAL:
        return await asyncio.to_thread(builder.build, run_id, parsed)
    capable = _require_transport_capable(builder, host, run_id)
    if host.kind is BuildHostKind.SSH:
        with ssh_build_transport_from_host(host, secret_registry) as transport:
            return await _run_over_transport(
                capable,
                transport,
                host=host,
                run_id=run_id,
                parsed=parsed,
                secret_registry=secret_registry,
            )
    if host.kind is BuildHostKind.EPHEMERAL_LIBVIRT:
        base_image = _require_base_image(host, run_id)
        with ephemeral_build_transport_from_host(
            base_image, secret_registry, run_id=run_id
        ) as transport:
            return await _run_over_transport(
                capable,
                transport,
                host=host,
                run_id=run_id,
                parsed=parsed,
                secret_registry=secret_registry,
            )
    raise CategorizedError(
        "unsupported build host kind",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "run_id": str(run_id),
            "build_host": host.name,
            "build_host_kind": str(host.kind),
        },
    )


def bind_over_transport(
    builder: TransportCapableBuilder,
    transport: BuildTransport,
    *,
    host_workspace_root: str,
    git_remote: str,
    git_ref: str,
    secret_registry: SecretRegistry,
) -> Builder:
    """Rebind ``builder`` onto ``transport`` with the host workspace and git coordinates."""
    return builder.over_transport(
        transport,
        host_workspace_root=host_workspace_root,
        git_remote=git_remote,
        git_ref=git_ref,
        secret_registry=secret_registry,
    )


async def _run_over_transport(
    builder: TransportCapableBuilder,
    transport: BuildTransport,
    *,
    host: BuildHost,
    run_id: UUID,
    parsed: ServerBuildProfile,
    secret_registry: SecretRegistry,
) -> BuildOutput:
    git_remote, git_ref = _git_coords(parsed, run_id)
    bound = bind_over_transport(
        builder,
        transport,
        host_workspace_root=host.workspace_root,
        git_remote=git_remote,
        git_ref=git_ref,
        secret_registry=secret_registry,
    )
    return await asyncio.to_thread(bound.build, run_id, parsed)


def _git_coords(parsed: ServerBuildProfile, run_id: UUID) -> tuple[str, str]:
    source = parsed.kernel_source_ref
    if not isinstance(source, GitKernelSource):
        raise CategorizedError(
            "remote build host requires a git kernel_source_ref",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id)},
        )
    return source.git.remote, source.git.ref


def _require_transport_capable(
    builder: Builder, host: BuildHost, run_id: UUID
) -> TransportCapableBuilder:
    if not isinstance(builder, TransportCapableBuilder):
        raise CategorizedError(
            "a remote build host requires a transport-capable builder",
            category=ErrorCategory.NOT_IMPLEMENTED,
            details={"run_id": str(run_id), "build_host": host.name},
        )
    return builder


def _require_base_image(host: BuildHost, run_id: UUID) -> str:
    if host.base_image_volume is None:
        raise CategorizedError(
            "ephemeral_libvirt build host has no base_image_volume",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"run_id": str(run_id), "build_host": host.name},
        )
    return host.base_image_volume
