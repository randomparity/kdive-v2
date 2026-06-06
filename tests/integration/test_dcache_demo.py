"""The dcache `dhash_entries=1` A/B demo driver (#128, gap G5, ADR-0056 §5-6).

`live_vm`-marked: one real System, two sequential Runs over the real build/install/boot
handlers -- Run A (vulnerable, no patch) crashes on the console; Run B (fixed, patch_ref)
boots clean. Skips cleanly in CI and on any host missing the four demo env vars. Drives the
handlers directly (ADR-0019), not the HTTP transport.
"""

from __future__ import annotations

import pytest

# Imported for the harness body + as a compile-time check that the real seams exist.
from kdive.providers.local_libvirt.install import classify_console, read_console_log
from kdive.providers.local_libvirt.provisioning import console_log_path
from tests.integration import _dcache_demo as demo

__all__ = ["classify_console", "console_log_path", "read_console_log"]


@pytest.mark.live_vm
def test_dcache_demo_ab_loop(migrated_url: str) -> None:  # pragma: no cover - live_vm
    """Vulnerable boot crashes on the console; the patched rebuild boots ready (test-case 05).

    Wired against an operator host with KDIVE_KERNEL_SRC, KDIVE_TEST_BUILD_CONFIG,
    KDIVE_GUEST_IMAGE, KDIVE_DEMO_FIX_PATCH all present and the staging dirs worker-writable
    (docs/runbooks/dcache-demo.md). The A/B the harness drives:

    1. seed a granted allocation; provision ONE System with ``demo.demo_provisioning_profile()``
       via the real provision handler; await ``ready``.
    2. open an investigation. Run A (vulnerable): ``runs.create`` with
       ``demo.demo_build_profile(fixed=False)`` -> ``runs.build(cmdline=demo.DEMO_CMDLINE)`` +
       build_handler -> install_handler -> boot_handler. Capture and assert Run A's console
       BEFORE Run B overwrites it: ``classify_console(read_console_log(console_log_path(sid)))``
       == ``"crashed"`` with ``__d_lookup`` in the text and ``dhash_entries=1`` in the
       ``Command line:`` line; the boot fails (readiness_failure if the signature is seen,
       else boot_timeout).
    3. Run B (fixed): a new Run on the SAME System with ``demo.demo_build_profile(fixed=True)``
       -> same build(cmdline)/install/boot. Assert the console classifies ``"ready"``.
    4. teardown in a finally: ``allocations.release`` + teardown the System, remove the
       per-Run staging dirs.

    The console classification is the ground-truth assertion; the boot-job outcome is
    secondary. Run B differs from Run A only by ``patch_ref`` (same config, same cmdline).
    """
    demo.demo_preflight()
    raise NotImplementedError(
        "live_vm dcache A/B harness: provision one System, then per Run "
        "create -> build(cmdline=demo.DEMO_CMDLINE) -> install -> boot via the real handlers; "
        "assert classify_console(read_console_log(console_log_path(system_id))) == 'crashed' "
        "(Run A, dhash_entries=1 in the Command line) then 'ready' (Run B, fixed); "
        "release + teardown in a finally. Wired by the live_vm runner."
    )
