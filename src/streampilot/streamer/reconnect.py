"""断流检测、指数退避重连与码率阶梯。

策略（与架构文档一致）：

- 退避：1s → 2s → 4s → 8s（上限），网络恢复后最迟 8 秒内重连，满足
  「断网 ≤ 15s 恢复」；
- 降档：每次失败降 400kbps，最低 800kbps；
- 切换：同一节点连续失败 3 次后切到备用节点，用尽后回到主节点；
- 回补：稳定运行 60 秒后升一档，回补以一次受控重启生效。

日志只记录节点序号与码率，不记录推流地址与密钥。
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Protocol

from streampilot.streamer.models import EncoderProfile, StreamTarget
from streampilot.streamer.supervisor import StreamerError

logger = logging.getLogger(__name__)

DEFAULT_STABLE_S = 60.0
DEFAULT_FAILOVER_AFTER = 3


@dataclass(frozen=True)
class BackoffPolicy:
    """指数退避参数。

    Attributes:
        base_s: 首次重试等待。
        factor: 退避倍数。
        max_s: 单次等待上限。

    Raises:
        ValueError: 参数非正，或上限小于首次等待。
    """

    base_s: float = 1.0
    factor: float = 2.0
    max_s: float = 8.0

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be positive")
        if self.factor < 1:
            raise ValueError("factor must be at least 1")
        if self.max_s < self.base_s:
            raise ValueError("max_s must not be smaller than base_s")

    def delay(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待时间。

        Args:
            attempt: 从 1 开始的连续失败次数。

        Returns:
            等待秒数，封顶 ``max_s``。

        Raises:
            ValueError: 次数小于 1。
        """
        if attempt < 1:
            raise ValueError("attempt must be at least 1")
        return min(self.base_s * self.factor ** (attempt - 1), self.max_s)


class RecoveryState(StrEnum):
    """重连器状态。"""

    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


class StreamerControl(Protocol):
    """重连器依赖的推流控制接口。"""

    @property
    def profile(self) -> EncoderProfile:
        """当前编码参数。"""
        ...

    def start(self, target: StreamTarget) -> int:
        """启动推流。"""
        ...

    def stop(self) -> None:
        """停止推流。"""
        ...

    def set_profile(self, profile: EncoderProfile) -> None:
        """更新编码参数。"""
        ...

    def health(self) -> dict[str, object]:
        """健康快照。"""
        ...


class StreamerRecovery:
    """推流断线重连与码率自适应。

    Args:
        supervisor: 推流进程控制。
        targets: 推流节点，第一个为主节点。
        policy: 退避策略。
        baseline: 基准编码参数，缺省取 supervisor 当前参数。
        stable_s: 稳定运行多久后回补一档。
        failover_after: 同节点连续失败多少次后切换节点。
        clock: 单调时钟，便于测试。

    Raises:
        ValueError: 节点为空、阈值非法或缺少基准参数。
    """

    def __init__(
        self,
        supervisor: StreamerControl,
        targets: Sequence[StreamTarget],
        *,
        policy: BackoffPolicy | None = None,
        baseline: EncoderProfile | None = None,
        stable_s: float = DEFAULT_STABLE_S,
        failover_after: int = DEFAULT_FAILOVER_AFTER,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not targets:
            raise ValueError("at least one stream target is required")
        if failover_after < 1:
            raise ValueError("failover_after must be at least 1")
        if stable_s <= 0:
            raise ValueError("stable_s must be positive")
        self._supervisor = supervisor
        self._targets = list(targets)
        self._policy = policy or BackoffPolicy()
        self._baseline = baseline or supervisor.profile
        self._stable_s = stable_s
        self._failover_after = failover_after
        self._clock = clock
        self._profile = self._baseline
        self._state = RecoveryState.STOPPED
        self._target_index = 0
        self._failures = 0
        self._node_failures = 0
        self._next_attempt_at: float | None = None
        self._running_since: float | None = None

    @property
    def state(self) -> RecoveryState:
        """当前状态。"""
        return self._state

    @property
    def profile(self) -> EncoderProfile:
        """当前生效的编码参数。"""
        return self._profile

    @property
    def failures(self) -> int:
        """连续失败次数。"""
        return self._failures

    @property
    def target(self) -> StreamTarget:
        """当前节点（``repr`` 已打码）。"""
        return self._targets[self._target_index]

    def start(self) -> None:
        """按当前档位启动主节点。"""
        self._failures = 0
        self._node_failures = 0
        self._next_attempt_at = None
        self._running_since = None
        self._supervisor.set_profile(self._profile)
        try:
            self._supervisor.start(self.target)
        except StreamerError:
            self._state = RecoveryState.RECONNECTING
            self._schedule(self._clock())
            return
        self._state = RecoveryState.RUNNING
        self._running_since = self._clock()

    def stop(self) -> None:
        """停止推流并关闭重连。"""
        self._state = RecoveryState.STOPPED
        self._next_attempt_at = None
        self._running_since = None
        self._supervisor.stop()

    def tick(self) -> RecoveryState:
        """推进一次状态机，返回当前状态。

        由调度器每 1~2 秒调用；同一 tick 内最多发起一次重启。

        Returns:
            当前状态。
        """
        if self._state is RecoveryState.STOPPED:
            return self._state
        if self._supervisor.health().get("state") == "running":
            self._on_healthy(self._clock())
        else:
            self._on_unhealthy(self._clock())
        return self._state

    def _on_healthy(self, now: float) -> None:
        """进程健康：清零失败并检查是否该回补码率。"""
        self._failures = 0
        self._node_failures = 0
        self._state = RecoveryState.RUNNING
        if self._running_since is None:
            self._running_since = now
        self._promote_if_due(now)

    def _on_unhealthy(self, now: float) -> None:
        """进程不可用：必要时安排重试并执行一次重启。"""
        self._running_since = None
        self._state = RecoveryState.RECONNECTING
        if self._next_attempt_at is None:
            self._schedule(now)
        due = self._next_attempt_at
        if due is None or now < due:
            return
        self._next_attempt_at = None
        try:
            self._supervisor.start(self.target)
        except StreamerError:
            self._schedule(now)

    def _schedule(self, now: float) -> None:
        """记录一次失败并安排下次重试。"""
        self._failures += 1
        self._node_failures += 1
        self._next_attempt_at = now + self._policy.delay(self._failures)
        self._degrade()
        if self._node_failures >= self._failover_after:
            self._rotate_target()
        logger.warning(
            "stream retry scheduled: attempt=%s delay=%.1fs bitrate=%skbps",
            self._failures,
            self._policy.delay(self._failures),
            self._profile.bitrate_kbps,
        )

    def _degrade(self) -> None:
        """失败后降一档码率。"""
        lowered = self._profile.next_lower()
        if lowered is None:
            return
        self._profile = lowered
        self._supervisor.set_profile(lowered)

    def _rotate_target(self) -> None:
        """切换到下一个节点。"""
        if len(self._targets) < 2:
            return
        self._target_index = (self._target_index + 1) % len(self._targets)
        self._node_failures = 0
        logger.warning(
            "stream target switched to node index %s", self._target_index
        )

    def _promote_if_due(self, now: float) -> None:
        """稳定运行达到阈值后回补一档码率。"""
        if self._running_since is None:
            return
        if now - self._running_since < self._stable_s:
            return
        higher = self._profile.next_higher(self._baseline.bitrate_kbps)
        if higher is None:
            return
        self._profile = higher
        self._supervisor.set_profile(higher)
        logger.info("stream bitrate restored to %skbps", higher.bitrate_kbps)
        self._restart_for_profile()

    def _restart_for_profile(self) -> None:
        """以受控重启让新码率立即生效。"""
        self._supervisor.stop()
        try:
            self._supervisor.start(self.target)
        except StreamerError:
            self._running_since = None
            self._state = RecoveryState.RECONNECTING
            self._schedule(self._clock())
            return
        self._running_since = self._clock()
