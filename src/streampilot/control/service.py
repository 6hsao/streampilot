"""人工接管与运行控制的核心入口。

实现 docs/ARCHITECTURE.md 第 3.2 节的接管路径：接管后停止消费发言队列、
清空待发声、通知下游静默；释放需填写处置结论；所有操作写入审计。
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter

from streampilot.contracts import ApiError, ErrorCode
from streampilot.control.actions import apply_control
from streampilot.control.lifecycle import LifecycleController
from streampilot.control.models import (
    AuditEntry,
    ControlRequest,
    ControlResult,
    ControlSnapshot,
    TakeoverAction,
    TakeoverResult,
)
from streampilot.control.records import AuditRecorder
from streampilot.control.session import SessionControl
from streampilot.control.store import AuditStore, TakeoverStore
from streampilot.control.takeover import TakeoverFlow
from streampilot.orchestrator.queue import QueuedTurn
from streampilot.orchestrator.state import SessionState

# 单次审计查询的最大条数
MAX_AUDIT_LIMIT = 200


class ControlService:
    """接管、运行控制与审计的统一入口。

    所有状态变更在同一把锁内完成，保证接管对编排器立即可见。

    Args:
        audit: 审计存储。
        takeovers: 接管记录存储。
        org_id: 组织标识，写入审计。
        clock: 时间源，便于测试注入。
    """

    def __init__(
        self,
        *,
        audit: AuditStore,
        takeovers: TakeoverStore,
        org_id: str = "org_default",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._audit = audit
        self._sessions: dict[str, SessionControl] = {}
        self._lock = threading.Lock()
        self._recorder = AuditRecorder(
            audit=audit,
            takeovers=takeovers,
            org_id=org_id,
            clock=self._clock,
        )
        self._takeover = TakeoverFlow(
            takeovers=takeovers,
            recorder=self._recorder,
            clock=self._clock,
        )
        self._lifecycle = LifecycleController(
            sessions=self._sessions,
            takeovers=takeovers,
            clock=self._clock,
        )

    def end_stream(self, stream_id: str, *, reason: str) -> None:
        """收播：结束状态机、清空队列并静默。

        Args:
            stream_id: 场次标识。
            reason: 收播原因码与说明。
        """
        with self._lock:
            self._lifecycle.end(stream_id, reason=reason)

    def reset_for_resume(self, stream_id: str) -> None:
        """恢复开播：为已收播场次重建状态机。

        Args:
            stream_id: 场次标识。
        """
        with self._lock:
            self._lifecycle.reset(stream_id)

    def register(self, control: SessionControl) -> None:
        """登记场次运行时。

        Args:
            control: 场次控制引用。

        Raises:
            ValueError: 场次标识重复。
        """
        with self._lock:
            if control.stream_id in self._sessions:
                raise ValueError(
                    f"stream already registered: {control.stream_id}"
                )
            self._sessions[control.stream_id] = control

    def is_ai_silent(self, stream_id: str) -> bool:
        """查询 AI 是否应完全静默。

        Args:
            stream_id: 场次标识。

        Returns:
            接管、暂停、静音或熔断时返回 True。
        """
        with self._lock:
            return self._require(stream_id).ai_silent

    def dequeue(self, stream_id: str, now_ms: int) -> QueuedTurn | None:
        """编排器唯一的取话入口，AI 静默时不出队。

        Args:
            stream_id: 场次标识。
            now_ms: 当前时间，毫秒时间戳。

        Returns:
            可发声的发言；静默、冷却或空队列时返回 None。
        """
        with self._lock:
            control = self._require(stream_id)
            if control.ai_silent:
                return None
            return control.queue.dequeue(now_ms)

    def takeover(
        self,
        stream_id: str,
        *,
        action: TakeoverAction,
        operator_id: str,
        reason: str,
    ) -> TakeoverResult:
        """接管或释放场次。

        Args:
            stream_id: 场次标识。
            action: take 或 release。
            operator_id: 操作人标识。
            reason: 接管原因；释放时作为处置结论。

        Returns:
            接管结果。

        Raises:
            ApiError: 场次不存在、已收播、重复接管或未接管即释放。
        """
        started = perf_counter()
        with self._lock:
            control = self._require(stream_id)
            operator = self._require_operator(operator_id)
            before = self._recorder.state_snapshot(control)
            if control.state.state is SessionState.ENDED:
                self._recorder.rejected(
                    stream_id,
                    operator,
                    f"takeover.{action}.rejected",
                    before,
                    ErrorCode.STATE_CONFLICT,
                    reason,
                )
                raise ApiError(ErrorCode.STATE_CONFLICT, "stream has ended")
            return self._takeover.apply(
                control,
                action=action,
                operator=operator,
                reason=reason,
                started=started,
                before=before,
            )

    def control(
        self,
        stream_id: str,
        *,
        request: ControlRequest,
        operator_id: str,
    ) -> ControlResult:
        """执行运行控制动作。

        Args:
            stream_id: 场次标识。
            request: 控制请求。
            operator_id: 操作人标识。

        Returns:
            控制结果。

        Raises:
            ApiError: 场次不存在、已收播或参数非法。
        """
        started = perf_counter()
        with self._lock:
            control = self._require(stream_id)
            operator = self._require_operator(operator_id)
            before = self._recorder.state_snapshot(control)
            if control.state.state is SessionState.ENDED:
                self._recorder.rejected(
                    stream_id,
                    operator,
                    f"control.{request.action}.rejected",
                    before,
                    ErrorCode.STATE_CONFLICT,
                    request.reason,
                )
                raise ApiError(ErrorCode.STATE_CONFLICT, "stream has ended")
            try:
                detail = apply_control(control, request)
            except ApiError as exc:
                self._recorder.rejected(
                    stream_id,
                    operator,
                    f"control.{request.action}.rejected",
                    before,
                    exc.code,
                    request.reason,
                )
                raise
            after = self._recorder.state_snapshot(control)
            after.update(detail)
            if request.reason:
                after["reason"] = request.reason
            self._recorder.record(
                stream_id,
                operator,
                f"control.{request.action}",
                before,
                after,
            )
            return ControlResult(
                stream_id=stream_id,
                action=request.action,
                state=control.state.state,
                ai_silent=control.ai_silent,
                latency_ms=(perf_counter() - started) * 1000,
            )

    def snapshot(self, stream_id: str) -> ControlSnapshot:
        """返回控制面视角的场次快照。

        Args:
            stream_id: 场次标识。

        Returns:
            状态、队列与接管信息。

        Raises:
            ApiError: 场次不存在。
        """
        with self._lock:
            control = self._require(stream_id)
            return ControlSnapshot(
                stream_id=stream_id,
                state=control.state.state,
                queue_depth=control.queue.stats().size,
                paused=control.paused,
                muted=control.muted,
                ai_silent=control.ai_silent,
                persona_version_id=control.persona_version_id,
                templates=sorted(control.templates),
                takeover=self._recorder.takeover_info(stream_id),
            )

    def audit(self, stream_id: str, limit: int = 50) -> list[AuditEntry]:
        """查询场次的操作审计。

        Args:
            stream_id: 场次标识。
            limit: 返回条数上限。

        Returns:
            时间倒序的审计记录。

        Raises:
            ApiError: 场次不存在或条数越界。
        """
        if not 1 <= limit <= MAX_AUDIT_LIMIT:
            raise ApiError(
                ErrorCode.INVALID_ARGUMENT,
                f"limit must be within 1..{MAX_AUDIT_LIMIT}",
            )
        with self._lock:
            self._require(stream_id)
            return self._audit.entries(stream_id, limit)

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

    def _require_operator(self, operator_id: str) -> str:
        """校验操作人标识。"""
        operator = operator_id.strip()
        if not operator:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "operator is required")
        return operator
