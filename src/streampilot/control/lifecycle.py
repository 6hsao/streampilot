"""场次生命周期与控制面运行时的衔接。

收播时结束状态机并清理发声；恢复开播时重建状态机。
调用方需持有控制面实例锁。
"""

from collections.abc import Callable
from datetime import datetime

from streampilot.contracts import ApiError, ErrorCode
from streampilot.control.session import SessionControl
from streampilot.control.store import TakeoverStore
from streampilot.orchestrator.state import (
    SessionState,
    SessionStateMachine,
)


class LifecycleController:
    """在不破坏静默语义的前提下切换场次运行状态。

    Args:
        sessions: 控制面场次表。
        takeovers: 接管记录存储。
        clock: 时间源。
    """

    def __init__(
        self,
        *,
        sessions: dict[str, SessionControl],
        takeovers: TakeoverStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._takeovers = takeovers
        self._clock = clock

    def end(self, stream_id: str, *, reason: str) -> None:
        """收播：结束状态机、清空队列并静默。

        Args:
            stream_id: 场次标识。
            reason: 收播原因，写入状态迁移历史。

        Raises:
            ApiError: 场次不存在或已收播。
        """
        control = self._require(stream_id)
        if control.state.state is SessionState.ENDED:
            raise ApiError(ErrorCode.STATE_CONFLICT, "stream already ended")
        active = self._takeovers.active(stream_id)
        if active is not None:
            self._takeovers.finish(
                stream_id,
                f"stream ended: {reason}",
                self._clock(),
            )
        control.queue.clear()
        control.paused = False
        control.muted = False
        control.resume_state = None
        control.state.transition(SessionState.ENDED, f"end:{reason}")
        control.sink.set_muted(False)
        control.sink.silence()

    def reset(self, stream_id: str) -> None:
        """恢复开播：为已收播场次重建状态机。

        Args:
            stream_id: 场次标识。

        Raises:
            ApiError: 场次不存在或尚未收播。
        """
        control = self._require(stream_id)
        if control.state.state is not SessionState.ENDED:
            raise ApiError(ErrorCode.STATE_CONFLICT, "stream is not ended")
        control.state = SessionStateMachine()
        control.queue.clear()
        control.paused = False
        control.muted = False
        control.resume_state = None
        control.sink.set_muted(False)
        control.sink.resume()

    def _require(self, stream_id: str) -> SessionControl:
        """取得场次，不存在时抛出 404。"""
        control = self._sessions.get(stream_id)
        if control is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "stream not found",
                details={"stream_id": stream_id},
            )
        return control
