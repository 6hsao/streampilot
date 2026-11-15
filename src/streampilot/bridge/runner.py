"""心跳循环：每 10 秒上报一次，中断即告警。

心跳中断意味着云端无法确认本机凭据桥在线，按架构约定应禁止新开播
并触发告警；已在播的场次不受影响。
"""

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from streampilot.bridge.client import BridgeError
from streampilot.bridge.models import BridgeHeartbeat

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_S = 10.0


class HeartbeatSink(Protocol):
    """能接收心跳的目标。"""

    def heartbeat(self, beat: BridgeHeartbeat) -> None:
        """上报一次心跳。"""
        ...


class BridgeRunner:
    """周期性上报心跳并统计失败。

    Args:
        client: 桥客户端。
        beat_factory: 构造心跳的函数，每次上报调用一次。
        interval_s: 心跳间隔（秒），默认 10 秒。
        on_alert: 断连首次触发时的告警回调。

    Raises:
        ValueError: 间隔非正数。
    """

    def __init__(
        self,
        client: HeartbeatSink,
        *,
        beat_factory: Callable[[], BridgeHeartbeat],
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        on_alert: Callable[[], None] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._client = client
        self._beat_factory = beat_factory
        self._interval_s = interval_s
        self._on_alert = on_alert
        self._alerted = False
        self.beats_sent = 0
        self.failures = 0

    @property
    def interval_s(self) -> float:
        """心跳间隔。"""
        return self._interval_s

    @property
    def alerting(self) -> bool:
        """当前是否处于断连告警状态。"""
        return self._alerted

    def beat_once(self) -> bool:
        """上报一次心跳。

        Returns:
            成功返回 True；失败返回 False 并（首次）触发告警。
        """
        try:
            self._client.heartbeat(self._beat_factory())
        except BridgeError as error:
            self.failures += 1
            if not self._alerted:
                self._alerted = True
                logger.warning("bridge heartbeat lost: %s", error)
                if self._on_alert is not None:
                    self._on_alert()
            return False
        self.beats_sent += 1
        self._alerted = False
        return True

    def serve(self, stop: threading.Event) -> None:
        """持续上报直到 ``stop`` 被置位。

        Args:
            stop: 停止信号。
        """
        while not stop.is_set():
            self.beat_once()
            stop.wait(self._interval_s)
