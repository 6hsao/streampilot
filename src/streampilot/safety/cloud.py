"""云内容安全：审核结果、供应商协议与配额控制。"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

Direction = Literal["input", "output"]
Status = Literal["pass", "review", "block"]


class ModerationUnavailableError(Exception):
    """云审核不可用（超时、限流、协议异常）。

    调用方必须按 fail-closed 处理：不发声并触发告警。
    """


@dataclass(frozen=True)
class CloudVerdict:
    """云审核结论。

    Attributes:
        status: 结论；``pass`` 放行、``review`` 复核、``block`` 拦截。
        labels: 命中的风险标签。
        provider: 供应商名称。
        latency_ms: 审核耗时。
    """

    status: Status
    labels: tuple[str, ...] = ()
    provider: str = "cloud"
    latency_ms: int = 0


class CloudModerator(Protocol):
    """云审核供应商协议。"""

    name: str

    def moderate(self, text: str, direction: Direction) -> CloudVerdict:
        """审核一段文本。

        Args:
            text: 待审核文本。
            direction: 审核方向，输入或输出。

        Returns:
            审核结论。

        Raises:
            ModerationUnavailableError: 调用失败或协议无法识别。
        """
        ...


class MinuteQuota:
    """按分钟计数的调用配额，用于控制云审核成本。

    Args:
        limit: 每分钟允许的调用次数。
        clock: 时钟函数，便于测试注入。

    Raises:
        ValueError: 配额非正。
    """

    def __init__(
        self, limit: int, clock: Callable[[], float] | None = None
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        self._limit = limit
        self._clock: Callable[[], float] = clock or time.monotonic
        self._window = -1
        self._used = 0

    @property
    def used(self) -> int:
        """当前窗口已用次数。"""
        return self._used

    def try_acquire(self) -> bool:
        """尝试占用一次配额。

        Returns:
            仍有配额返回 True，超出返回 False。
        """
        window = int(self._clock() // 60)
        if window != self._window:
            self._window = window
            self._used = 0
        if self._used >= self._limit:
            return False
        self._used += 1
        return True
