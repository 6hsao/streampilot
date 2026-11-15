"""安全熔断器的触发、恢复与全局/按场组合的测试。"""

import pytest

from streampilot.safety import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitRegistry,
    CircuitState,
)


class FakeClock:
    """可推进的时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_consecutive_failures_trip_circuit() -> None:
    """连续失败达到阈值立即熔断。"""
    breaker = CircuitBreaker(failure_threshold=3, clock=FakeClock())

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is False

    breaker.record_failure()

    snapshot = breaker.snapshot()
    assert snapshot["state"] == "open"
    assert "error rate" in str(snapshot["reason"])


def test_success_breaks_consecutive_run() -> None:
    """成功会打断连续失败计数。"""
    breaker = CircuitBreaker(
        failure_threshold=3, error_rate=1.0, clock=FakeClock()
    )

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.is_open is False


def test_error_rate_trips_with_mixed_traffic() -> None:
    """窗口内错误率超阈值同样触发。"""
    breaker = CircuitBreaker(
        failure_threshold=4, error_rate=0.5, clock=FakeClock()
    )

    for failed in (True, False, True, True):
        if failed:
            breaker.record_failure()
        else:
            breaker.record_success()

    assert breaker.is_open is True


def test_manual_reset_recovers() -> None:
    """人工确认后恢复闭合。"""
    breaker = CircuitBreaker(clock=FakeClock())
    breaker.trip("manual")

    with pytest.raises(CircuitOpenError):
        breaker.check()

    breaker.reset("operator-1")

    assert breaker.state is CircuitState.CLOSED
    breaker.check()


def test_cooldown_allows_half_open_probe() -> None:
    """冷却期后进入半开，成功即闭合。"""
    clock = FakeClock()
    breaker = CircuitBreaker(cooldown_seconds=30.0, clock=clock)
    breaker.trip("test")

    clock.advance(10)
    assert breaker.allow() is False

    clock.advance(25)
    assert breaker.allow() is True
    assert breaker.state.value == "half_open"

    breaker.record_success()

    assert breaker.state.value == "closed"


def test_half_open_failure_reopens() -> None:
    """半开探测失败重新打开。"""
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_seconds=10.0, clock=clock
    )
    breaker.trip("test")
    clock.advance(11)
    assert breaker.allow()

    breaker.record_failure()

    assert breaker.is_open is True


def test_snapshot_reports_window_metrics() -> None:
    """快照含窗口事件数与错误率。"""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=10, clock=clock)
    breaker.record_success()
    breaker.record_failure()

    snapshot = breaker.snapshot()

    assert snapshot["state"] == "closed"
    assert snapshot["window_events"] == 2
    assert snapshot["window_failures"] == 1
    assert snapshot["error_rate"] == 0.5


def test_events_outside_window_are_dropped() -> None:
    """窗口外的历史事件不再参与统计。"""
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=2, window_seconds=10.0, clock=clock
    )
    breaker.record_failure()
    clock.advance(20)
    breaker.record_failure()

    assert breaker.is_open is False
    assert breaker.snapshot()["window_events"] == 1


def test_registry_checks_global_and_session() -> None:
    """全局熔断影响所有场次，场次熔断只影响本场。"""
    registry = CircuitRegistry()
    registry.global_breaker.trip("cloud down")

    with pytest.raises(CircuitOpenError):
        registry.check("ss_1")

    registry = CircuitRegistry()
    registry.for_session("ss_1").trip("local")
    registry.check("ss_2")

    with pytest.raises(CircuitOpenError):
        registry.check("ss_1")


def test_invalid_parameters_and_operator() -> None:
    """非法参数与空操作人被拒绝。"""
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(window_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreaker(error_rate=0)
    with pytest.raises(ValueError):
        CircuitBreaker(cooldown_seconds=-1)
    with pytest.raises(ValueError):
        CircuitBreaker().reset("  ")
