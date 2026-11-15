"""安全熔断：错误率与高危命中触发，恢复需人工或冷却确认。

设计要点：
- 熔断状态是全局或按场次的开关，编排器在每次发声前检查；
- 触发后立刻停止发声，不等待当前流结束；
- 恢复默认需要人工确认；允许配置冷却期后自动半开探测。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    """熔断器状态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """熔断打开时拒绝发声。"""


@dataclass
class CircuitBreaker:
    """按错误率与高危命中触发的熔断器。

    Args:
        failure_threshold: 触发熔断的连续失败次数。
        window_seconds: 统计错误率的时间窗口。
        error_rate: 窗口内错误率阈值，0~1。
        cooldown_seconds: 打开后允许半开探测的冷却时间；0 表示只能人工恢复。
        clock: 时间源，便于测试注入。

    Raises:
        ValueError: 参数越界。
    """

    failure_threshold: int = 3
    window_seconds: float = 60.0
    error_rate: float = 0.3
    cooldown_seconds: float = 0.0
    clock: Callable[[], float] = time.monotonic
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0
    reason: str = ""
    _events: list[tuple[float, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if not 0.0 < self.error_rate <= 1.0:
            raise ValueError("error_rate must be within (0, 1]")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")

    @property
    def is_open(self) -> bool:
        """当前是否禁止发声。"""
        return self.state is CircuitState.OPEN

    def record_success(self) -> None:
        """记录一次成功；半开状态下成功即闭合。"""
        if self.state is CircuitState.HALF_OPEN:
            self._close()
            return
        self._record(False)

    def record_failure(self) -> None:
        """记录一次失败，必要时打开熔断。"""
        self._record(True)
        if self.state is CircuitState.OPEN:
            return
        recent = self._recent()
        failures = sum(1 for _, failed in recent if failed)
        consecutive = 0
        for _, failed in reversed(recent):
            if not failed:
                break
            consecutive += 1
        rate = failures / len(recent) if recent else 0.0
        rate_exceeded = (
            len(recent) >= self.failure_threshold and rate >= self.error_rate
        )
        if consecutive >= self.failure_threshold or rate_exceeded:
            self.trip(f"error rate {rate:.2f} over {len(recent)} events")

    def trip(self, reason: str) -> None:
        """立即打开熔断。

        Args:
            reason: 触发原因，写入告警与审计。
        """
        self.state = CircuitState.OPEN
        self.opened_at = self.clock()
        self.reason = reason

    def allow(self) -> bool:
        """判断当前是否允许发声。

        冷却期已过时会切换到半开，允许一次探测。

        Returns:
            允许返回 True。
        """
        if self.state is not CircuitState.OPEN:
            return True
        if self.cooldown_seconds <= 0:
            return False
        if self.clock() - self.opened_at < self.cooldown_seconds:
            return False
        self.state = CircuitState.HALF_OPEN
        return True

    def check(self) -> None:
        """不满足发声条件时抛出异常。

        Raises:
            CircuitOpenError: 熔断打开或冷却未结束。
        """
        if not self.allow():
            raise CircuitOpenError(self.reason or "circuit is open")

    def reset(self, operator: str) -> None:
        """人工确认后恢复。

        Args:
            operator: 确认人标识。

        Raises:
            ValueError: 操作人为空。
        """
        if not operator.strip():
            raise ValueError("operator must not be empty")
        self._events.clear()
        self._close(reason=f"reset by {operator}")

    def snapshot(self) -> dict[str, object]:
        """返回可上报的状态快照。"""
        recent = self._recent()
        failures = sum(1 for _, failed in recent if failed)
        return {
            "state": self.state.value,
            "reason": self.reason,
            "window_events": len(recent),
            "window_failures": failures,
            "error_rate": (failures / len(recent)) if recent else 0.0,
        }

    def _record(self, failed: bool) -> None:
        self._events.append((self.clock(), failed))

    def _recent(self) -> list[tuple[float, bool]]:
        """截取时间窗口内的事件。"""
        cutoff = self.clock() - self.window_seconds
        self._events = [item for item in self._events if item[0] >= cutoff]
        return self._events

    def _close(self, reason: str = "") -> None:
        self.state = CircuitState.CLOSED
        self.reason = reason
        self.opened_at = 0.0


class CircuitRegistry:
    """管理全局与按场次熔断器。

    Args:
        breaker_factory: 熔断器工厂，便于统一配置参数。
    """

    def __init__(
        self, breaker_factory: Callable[[], CircuitBreaker] | None = None
    ) -> None:
        self._factory = breaker_factory or CircuitBreaker
        self.global_breaker = self._factory()
        self._sessions: dict[str, CircuitBreaker] = {}

    def for_session(self, session_id: str) -> CircuitBreaker:
        """取得场次熔断器，不存在时创建。

        Args:
            session_id: 场次标识。

        Returns:
            该场次的熔断器。
        """
        breaker = self._sessions.get(session_id)
        if breaker is None:
            breaker = self._factory()
            self._sessions[session_id] = breaker
        return breaker

    def check(self, session_id: str) -> None:
        """全局与场次任一熔断即拒绝发声。

        Args:
            session_id: 场次标识。

        Raises:
            CircuitOpenError: 任一熔断打开。
        """
        self.global_breaker.check()
        self.for_session(session_id).check()
