"""Remote-libvirt crash-postmortem adapter wiring."""

from __future__ import annotations

from kdive.providers.debug_common.crash_postmortem import (
    FetchObject,
    ReadBuildId,
    RunCrash,
)
from kdive.providers.debug_common.crash_postmortem import (
    run_crash_postmortem as _run_crash_postmortem,
)
from kdive.providers.ports import CrashOutput
from kdive.security.secrets.secret_registry import SecretRegistry


class CrashPostmortemAdapter:
    """Provider-neutral crash postmortem wiring for remote-libvirt vmcore refs."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        fetch_object: FetchObject,
        read_build_id: ReadBuildId,
        run_crash: RunCrash,
    ) -> None:
        self._secret_registry = secret_registry
        self._fetch_object = fetch_object
        self._read_build_id = read_build_id
        self._run_crash = run_crash

    def run(
        self,
        *,
        vmcore_ref: str,
        debuginfo_ref: str,
        expected_build_id: str,
        commands: list[str],
    ) -> CrashOutput:
        return _run_crash_postmortem(
            vmcore_ref=vmcore_ref,
            debuginfo_ref=debuginfo_ref,
            expected_build_id=expected_build_id,
            commands=commands,
            fetch_object=self._fetch_object,
            read_build_id=self._read_build_id,
            run_crash=self._run_crash,
            secret_registry=self._secret_registry,
        )
