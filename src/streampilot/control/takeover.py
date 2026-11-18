"""人工接管与释放的状态迁移。"""

from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from typing import Any

from streampilot.contracts import ApiError, ErrorCode
from streampilot.contracts.ids import new_id
from streampilot.control.models import (
    TakeoverAction,
    TakeoverInfo,
    TakeoverResult,
)
from streampilot.control.records import AuditRecorder
from streampilot.control.session import SessionControl
from streampilot.control.store import TakeoverRecord, TakeoverStore
from streampilot.orchestrator.state import (
    InvalidTransition,
    SessionState,
)


class TakeoverFlow:
    """执行接管与释放，并保证 AI 静默语义。

    Args:
        takeovers: 接管记录存储。
        recorder: 审计记录器。
        clock: 时间源。
    """

    def __init__(
        self,
        *,
        takeovers: TakeoverStore,
        recorder: AuditRecorder,
        clock: Callable[[], datetime],
    ) -> None:
        self._takeovers = takeovers
        self._recorder = recorder
        self._clock = clock

    def apply(
        self,
        control: SessionControl,
        *,
        action: TakeoverAction,
        operator: str,
        reason: str,
        started: float,
        before: dict[str, Any],
    ) -> TakeoverResult:
        """按动作执行接管或释放。

        Args:
            control: 场次运行时。
            action: take 或 release。
            operator: 操作人标识。
            reason: 接管原因；释放时作为处置结论。
            started: 请求开始时间，用于计算耗时。
            before: 操作前状态快照。

        Returns:
            接管结果。
        """
        if action is TakeoverAction.TAKE:
            return self._take(control, operator, reason, started, before)
        return self._release(control, operator, reason, started, before)

    def _take(
        self,
        control: SessionControl,
        operator: str,
        reason: str,
        started: float,
        before: dict[str, Any],
    ) -> TakeoverResult:
        """执行接管：置状态、清空队列、通知下游静默。"""
        if control.state.state is SessionState.TAKEOVER:
            self._recorder.rejected(
                control.stream_id,
                operator,
                "takeover.take.rejected",
                before,
                ErrorCode.ALREADY_TAKEOVER,
                reason,
            )
            raise ApiError(
                ErrorCode.ALREADY_TAKEOVER, "already under takeover"
            )
        record = TakeoverRecord(
            id=new_id(),
            session_id=control.stream_id,
            operator_id=operator,
            reason=reason,
            started_at=self._clock(),
        )
        self._takeovers.start(record)
        control.resume_state = control.state.state
        try:
            control.state.takeover(operator)
        except InvalidTransition as exc:
            raise ApiError(
                ErrorCode.STATE_CONFLICT, "illegal takeover transition"
            ) from exc
        # 接管优先：清空待发声并立即静默，暂停与静音标志一并复位
        cleared = control.queue.clear()
        control.paused = False
        control.muted = False
        control.sink.set_muted(False)
        control.sink.silence()
        after = self._recorder.state_snapshot(control)
        after["queue_cleared"] = cleared
        after["reason"] = reason
        self._recorder.record(
            control.stream_id, operator, "takeover.take", before, after
        )
        return TakeoverResult(
            stream_id=control.stream_id,
            action=TakeoverAction.TAKE,
            state=control.state.state,
            takeover=self._recorder.takeover_info(control.stream_id),
            cleared_queue=cleared,
            ai_silent=control.ai_silent,
            latency_ms=(perf_counter() - started) * 1000,
        )

    def _release(
        self,
        control: SessionControl,
        operator: str,
        conclusion: str,
        started: float,
        before: dict[str, Any],
    ) -> TakeoverResult:
        """释放接管：写处置结论，必要时恢复熔断语义。"""
        if control.state.state is not SessionState.TAKEOVER:
            self._recorder.rejected(
                control.stream_id,
                operator,
                "takeover.release.rejected",
                before,
                ErrorCode.STATE_CONFLICT,
                conclusion,
            )
            raise ApiError(ErrorCode.STATE_CONFLICT, "no active takeover")
        record = self._takeovers.finish(
            control.stream_id, conclusion, self._clock()
        )
        if record is None:
            raise ApiError(ErrorCode.STATE_CONFLICT, "no active takeover")
        restore = control.resume_state
        control.resume_state = None
        control.state.transition(SessionState.IDLE, f"release:{operator}")
        if restore is SessionState.CIRCUIT_OPEN:
            # 接管前处于熔断：释放后回到熔断，仍需人工复位才能发声
            control.state.transition(
                SessionState.CIRCUIT_OPEN, f"restore:{operator}"
            )
        control.paused = False
        control.muted = False
        control.sink.set_muted(False)
        cleared = control.queue.clear()
        if restore is not SessionState.CIRCUIT_OPEN:
            control.sink.resume()
        after = self._recorder.state_snapshot(control)
        after["queue_cleared"] = cleared
        after["conclusion"] = conclusion
        self._recorder.record(
            control.stream_id,
            operator,
            "takeover.release",
            before,
            after,
        )
        return TakeoverResult(
            stream_id=control.stream_id,
            action=TakeoverAction.RELEASE,
            state=control.state.state,
            takeover=TakeoverInfo(
                active=False,
                operator_id=record.operator_id,
                reason=record.reason,
                started_at=record.started_at,
                conclusion=record.conclusion,
            ),
            cleared_queue=cleared,
            ai_silent=control.ai_silent,
            latency_ms=(perf_counter() - started) * 1000,
        )
