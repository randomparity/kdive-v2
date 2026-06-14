"""Build provider port protocol tests."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from kdive.profiles.build import ServerBuildProfile
from kdive.provider_components.build_results import BuildOutput
from kdive.providers.ports.build import Builder, TransportCapableBuilder
from kdive.providers.ports.build_transport import BuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry


class _PlainBuilder:
    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        return BuildOutput(kernel_ref="kernel", debuginfo_ref="vmlinux", build_id=str(run_id))


class _TransportBuilder(_PlainBuilder):
    def over_transport(
        self,
        transport: BuildTransport,
        *,
        host_workspace_root: str,
        git_remote: str,
        git_ref: str,
        secret_registry: SecretRegistry,
    ) -> Builder:
        del transport, host_workspace_root, git_remote, git_ref, secret_registry
        return self


def test_transport_capable_builder_is_runtime_checkable() -> None:
    plain = _PlainBuilder()
    capable = _TransportBuilder()

    assert not isinstance(plain, TransportCapableBuilder)
    assert isinstance(capable, TransportCapableBuilder)
    assert (
        capable.over_transport(
            cast(BuildTransport, object()),
            host_workspace_root="/work",
            git_remote="https://example.invalid/linux.git",
            git_ref="main",
            secret_registry=SecretRegistry(),
        )
        is capable
    )
