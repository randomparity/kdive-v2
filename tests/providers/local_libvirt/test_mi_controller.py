"""pygdbmi controller adapter tests without spawning gdb."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.debug_common.mi_controller import PygdbmiController, timeout_error


class _FakeGdbController:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.writes: list[tuple[str, float, bool]] = []
        self.reads: list[tuple[float, bool]] = []
        self.exited = False

    def write(
        self,
        command: str,
        *,
        timeout_sec: float,
        raise_error_on_timeout: bool,
    ) -> list[dict[str, object]]:
        self.writes.append((command, timeout_sec, raise_error_on_timeout))
        if self.timeout:
            from pygdbmi.constants import GdbTimeoutError

            raise GdbTimeoutError("timed out")
        return [{"type": "result", "message": "done"}]

    def get_gdb_response(
        self, *, timeout_sec: float, raise_error_on_timeout: bool = True
    ) -> list[dict[str, object]]:
        self.reads.append((timeout_sec, raise_error_on_timeout))
        if self.timeout:
            from pygdbmi.constants import GdbTimeoutError

            raise GdbTimeoutError("timed out")
        return [{"type": "notify", "message": "stopped"}]

    def exit(self) -> None:
        self.exited = True


def _controller(fake: _FakeGdbController) -> PygdbmiController:
    controller = object.__new__(PygdbmiController)
    controller._controller = fake
    return controller


def test_timeout_error_preserves_command_timeout_and_transport_code() -> None:
    error = timeout_error("-exec-continue", 1.5)

    assert error.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert error.details == {
        "code": "transport_stall",
        "command": "-exec-continue",
        "timeout_seconds": 1.5,
    }


def test_write_forwards_timeout_and_requests_timeout_errors() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    records = controller.write("-break-list", timeout_sec=2.0)

    assert records == [{"type": "result", "message": "done"}]
    assert fake.writes == [("-break-list", 2.0, True)]


def test_write_maps_pygdbmi_timeout_to_categorized_error() -> None:
    controller = _controller(_FakeGdbController(timeout=True))

    with pytest.raises(CategorizedError) as exc_info:
        controller.write("-target-select remote 127.0.0.1:1234", timeout_sec=0.25)

    assert exc_info.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc_info.value.details["command"] == "-target-select remote 127.0.0.1:1234"


def test_read_uses_non_raising_poll() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    assert controller.read(timeout_sec=0.1) == [{"type": "notify", "message": "stopped"}]
    assert fake.reads == [(0.1, False)]


def test_get_gdb_response_maps_or_suppresses_timeout() -> None:
    controller = _controller(_FakeGdbController(timeout=True))

    with pytest.raises(CategorizedError) as exc_info:
        controller.get_gdb_response(timeout_sec=0.5)

    assert exc_info.value.details["command"] == "get_gdb_response"
    assert controller.get_gdb_response(timeout_sec=0.5, raise_error_on_timeout=False) == []


def test_exit_delegates_to_underlying_controller() -> None:
    fake = _FakeGdbController()
    controller = _controller(fake)

    controller.exit()

    assert fake.exited is True
