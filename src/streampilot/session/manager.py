"""场次管理：创建、开播、收播、恢复与配置快照。

场次通过控制面登记运行时；收播结束状态机，恢复时重建。
首发使用进程内存储，接入 MySQL / Redis 后按同一接口替换。
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from streampilot.contracts import ApiError, ErrorCode
from streampilot.contracts.ids import new_id
from streampilot.control.models import AuditEntry
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.sink import LoggingControlSink
from streampilot.control.store import AuditStore
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.state import SessionStateMachine
from streampilot.persona.models import PersonaSnapshot
from streampilot.session.models import (
    SessionConfig,
    SessionStateError,
    SessionStatus,
    StreamSession,
)
from streampilot.session.views import (
    CreateStreamRequest,
    ResumeStreamRequest,
    SessionView,
    StartStreamRequest,
    StopStreamRequest,
)

# 运行时工厂：按场次配置构造控制面运行时
RuntimeFactory = Callable[[str, SessionConfig], SessionControl]


class SessionManager:
    """场次生命周期与配置快照的管理入口。

    Args:
        control: 控制面服务，用于登记与切换场次运行时。
        audit: 审计存储。
        org_id: 组织标识。
        clock: 时间源，便于测试注入。
        runtime_factory: 运行时工厂，缺省使用内存队列与日志下游。
        templates: 预设话术，注入新建场次。
    """

    def __init__(
        self,
        *,
        control: ControlService,
        audit: AuditStore,
        org_id: str = "org_default",
        clock: Callable[[], datetime] | None = None,
        runtime_factory: RuntimeFactory | None = None,
        templates: Mapping[str, str] | None = None,
    ) -> None:
        self._control = control
        self._audit = audit
        self._org_id = org_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._templates = dict(templates or {})
        self._runtime_factory = runtime_factory or self._default_runtime
        self._sessions: dict[str, StreamSession] = {}

    def create(
        self, request: CreateStreamRequest, *, operator: str
    ) -> SessionView:
        """创建场次并登记控制面运行时。

        Args:
            request: 创建请求。
            operator: 操作人。

        Returns:
            含配置快照的场次视图。

        Raises:
            ApiError: 配置非法。
        """
        actor = self._require_operator(operator)
        stream_id = new_id()
        session_id = new_id()
        try:
            config = SessionConfig(
                persona=PersonaSnapshot(
                    persona_id=request.persona_id,
                    version=request.persona_version,
                    prompt_digest=request.prompt_digest,
                ),
                voice_id=request.voice_id,
                avatar_id=request.avatar_id,
                title=request.title,
                platform_account_id=request.platform_account_id,
                room_ref=request.room_ref,
            )
        except ValueError as exc:
            raise ApiError(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        session = StreamSession(
            session_id=session_id,
            config=config,
            created_at=self._clock(),
        )
        runtime = self._runtime_factory(stream_id, config)
        self._control.register(runtime)
        self._sessions[stream_id] = session
        self._record(
            stream_id,
            actor,
            "session.create",
            None,
            {
                "session_id": session_id,
                "status": session.status.value,
                "persona_version": config.persona.version,
                "prompt_digest": config.persona.prompt_digest,
            },
        )
        return self._view(stream_id, session)

    def start(
        self,
        stream_id: str,
        *,
        request: StartStreamRequest,
        operator: str,
        checks: Mapping[str, bool] | None = None,
    ) -> SessionView:
        """开播；自检结果由调用方提供，缺省视为通过。

        Args:
            stream_id: 场次标识。
            request: 开播请求。
            operator: 操作人。
            checks: 合规自检结果，由 LIV-07 接入。

        Returns:
            更新后的场次视图。

        Raises:
            ApiError: 场次不存在或状态非法。
        """
        session = self._require(stream_id)
        actor = self._require_operator(operator)
        before = self._status_snapshot(session)
        try:
            session.start(checks or {}, actor, now=self._clock())
        except SessionStateError as exc:
            self._reject(stream_id, actor, "session.start", before, exc)
            raise ApiError(ErrorCode.STATE_CONFLICT, str(exc)) from exc
        self._record(
            stream_id,
            actor,
            "session.start",
            before,
            {
                "status": session.status.value,
                "expected_start_at": (
                    request.expected_start_at.isoformat()
                    if request.expected_start_at
                    else None
                ),
            },
        )
        return self._view(stream_id, session)

    def stop(
        self,
        stream_id: str,
        *,
        request: StopStreamRequest,
        operator: str,
    ) -> SessionView:
        """收播；结束控制面状态机并记录原因码。

        Args:
            stream_id: 场次标识。
            request: 收播请求。
            operator: 操作人或 system。

        Returns:
            更新后的场次视图。

        Raises:
            ApiError: 场次不存在或非直播中。
        """
        session = self._require(stream_id)
        actor = self._require_operator(operator)
        before = self._status_snapshot(session)
        if session.status is not SessionStatus.LIVE:
            self._reject(
                stream_id,
                actor,
                "session.stop",
                before,
                SessionStateError(f"cannot stop from {session.status}"),
            )
            raise ApiError(
                ErrorCode.STATE_CONFLICT,
                f"cannot stop from {session.status}",
            )
        self._control.end_stream(
            stream_id,
            reason=f"{request.reason.value}:{request.detail}",
        )
        session.stop(request.reason, request.detail, actor, now=self._clock())
        self._record(
            stream_id,
            actor,
            "session.stop",
            before,
            {
                "status": session.status.value,
                "reason": request.reason.value,
                "detail": request.detail,
            },
        )
        return self._view(stream_id, session)

    def resume(
        self,
        stream_id: str,
        *,
        request: ResumeStreamRequest,
        operator: str,
    ) -> SessionView:
        """恢复开播；复用配置快照并重建控制面状态机。

        Args:
            stream_id: 场次标识。
            request: 恢复请求。
            operator: 操作人。

        Returns:
            更新后的场次视图。

        Raises:
            ApiError: 场次不存在或不是已收播状态。
        """
        session = self._require(stream_id)
        actor = self._require_operator(operator)
        before = self._status_snapshot(session)
        if session.status is not SessionStatus.STOPPED:
            self._reject(
                stream_id,
                actor,
                "session.resume",
                before,
                SessionStateError(f"cannot resume from {session.status}"),
            )
            raise ApiError(
                ErrorCode.STATE_CONFLICT,
                f"cannot resume from {session.status}",
            )
        self._control.reset_for_resume(stream_id)
        session.resume(actor, now=self._clock())
        self._record(
            stream_id,
            actor,
            "session.resume",
            before,
            {
                "status": session.status.value,
                "reason": request.reason,
            },
        )
        return self._view(stream_id, session)

    def _view(self, stream_id: str, session: StreamSession) -> SessionView:
        """构造场次视图，状态取控制面实时值。"""
        snapshot = self._control.snapshot(stream_id)
        config = session.config
        return SessionView(
            stream_id=stream_id,
            session_id=session.session_id,
            status=session.status,
            state=snapshot.state,
            title=config.title,
            persona_id=config.persona.persona_id,
            persona_version=config.persona.version,
            prompt_digest=config.persona.prompt_digest,
            voice_id=config.voice_id,
            avatar_id=config.avatar_id,
            platform_account_id=config.platform_account_id,
            room_ref=config.room_ref,
            created_at=session.created_at,
            started_at=session.started_at,
            ended_at=session.ended_at,
            stop_reason=session.stop_reason,
            stop_detail=session.stop_detail,
        )

    def _default_runtime(
        self, stream_id: str, config: SessionConfig
    ) -> SessionControl:
        """构造内存运行时；生产由部署侧注入 Redis 队列实现。"""
        return SessionControl(
            stream_id=stream_id,
            state=SessionStateMachine(),
            queue=InMemorySpeechQueue(),
            sink=LoggingControlSink(),
            templates=self._templates,
            persona_version_id=(
                f"{config.persona.persona_id}:v{config.persona.version}"
            ),
        )

    def _require(self, stream_id: str) -> StreamSession:
        """取得场次，不存在时抛出 404。"""
        session = self._sessions.get(stream_id)
        if session is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "stream not found",
                details={"stream_id": stream_id},
            )
        return session

    def _require_operator(self, operator: str) -> str:
        """校验操作人标识。"""
        actor = operator.strip()
        if not actor:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "operator is required")
        return actor

    def _status_snapshot(self, session: StreamSession) -> dict[str, Any]:
        """构造写入审计的状态快照。"""
        return {
            "status": session.status.value,
            "stop_reason": (
                session.stop_reason.value if session.stop_reason else None
            ),
        }

    def _reject(
        self,
        stream_id: str,
        actor: str,
        action: str,
        before: dict[str, Any],
        exc: Exception,
    ) -> None:
        """记录被拒绝的场次操作。"""
        self._record(
            stream_id,
            actor,
            f"{action}.rejected",
            before,
            {"error": "STATE_CONFLICT", "message": str(exc)},
        )

    def _record(
        self,
        stream_id: str,
        actor: str,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """写入审计记录。"""
        self._audit.append(
            AuditEntry(
                id=new_id(),
                org_id=self._org_id,
                actor_id=actor,
                action=action,
                target=stream_id,
                before=before,
                after=after,
                created_at=self._clock(),
            )
        )
