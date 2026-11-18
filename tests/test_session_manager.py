"""场次管理器的单元测试。"""

from datetime import UTC, datetime

import pytest

from streampilot.contracts import ApiError, ErrorCode
from streampilot.control.models import TakeoverAction
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.state import (
    SessionState,
    SessionStateMachine,
)
from streampilot.session.manager import SessionManager
from streampilot.session.models import (
    SessionConfig,
    SessionStatus,
    StopReason,
)
from streampilot.session.views import (
    CreateStreamRequest,
    ResumeStreamRequest,
    SessionView,
    StartStreamRequest,
    StopStreamRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeSink:
    """记录控制指令的假下游。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def silence(self) -> None:
        """记录静默。"""
        self.calls.append("silence")

    def resume(self) -> None:
        """记录恢复。"""
        self.calls.append("resume")

    def set_muted(self, muted: bool) -> None:
        """记录静音切换。"""
        self.calls.append(f"muted:{muted}")

    def switch_persona(self, persona_version_id: str) -> None:
        """记录人设切换。"""
        self.calls.append("switch_persona")

    def speak_template(self, template_id: str, text: str) -> None:
        """记录话术播放。"""
        self.calls.append("speak_template")

    def reconnect_stream(self) -> None:
        """记录重连。"""
        self.calls.append("reconnect")


class Harness:
    """测试装置。"""

    def __init__(self) -> None:
        self.audit = InMemoryAuditStore()
        self.control = ControlService(
            audit=self.audit,
            takeovers=InMemoryTakeoverStore(),
            clock=lambda: NOW,
        )
        self.sinks: dict[str, FakeSink] = {}
        self.manager = SessionManager(
            control=self.control,
            audit=self.audit,
            clock=lambda: NOW,
            runtime_factory=self._factory,
        )

    def _factory(
        self, stream_id: str, config: SessionConfig
    ) -> SessionControl:
        sink = FakeSink()
        self.sinks[stream_id] = sink
        return SessionControl(
            stream_id=stream_id,
            state=SessionStateMachine(),
            queue=InMemorySpeechQueue(cooldown_ms=0),
            sink=sink,
        )

    def create(self) -> SessionView:
        """创建演示场次。"""
        return self.manager.create(
            CreateStreamRequest(
                persona_id="p_1",
                persona_version=2,
                prompt_digest="digest_abc",
                voice_id="voice_1",
                avatar_id="avatar_1",
                title="晚间答疑",
                platform_account_id="acc_1",
                room_ref="room_1",
            ),
            operator="op_1",
        )

    def actions(self, stream_id: str) -> list[str]:
        """返回审计动作名。"""
        return [entry.action for entry in self.audit.entries(stream_id)]


def test_create_builds_config_snapshot() -> None:
    """创建场次返回完整配置快照并登记运行时。"""
    harness = Harness()

    view = harness.create()

    assert view.stream_id
    assert view.session_id
    assert view.stream_id != view.session_id
    assert view.status is SessionStatus.CREATED
    assert view.state is SessionState.IDLE
    assert view.persona_id == "p_1"
    assert view.persona_version == 2
    assert view.prompt_digest == "digest_abc"
    assert view.title == "晚间答疑"
    assert view.room_ref == "room_1"
    assert view.created_at == NOW
    assert harness.control.snapshot(view.stream_id).state is (
        SessionState.IDLE
    )
    assert harness.actions(view.stream_id) == ["session.create"]


def test_start_and_duplicate_start() -> None:
    """开播成功，重复开播被拒绝并留痕。"""
    harness = Harness()
    stream_id = harness.create().stream_id

    view = harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )

    assert view.status is SessionStatus.LIVE
    assert view.started_at == NOW

    with pytest.raises(ApiError) as excinfo:
        harness.manager.start(
            stream_id,
            request=StartStreamRequest(),
            operator="op_1",
        )
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT
    assert harness.actions(stream_id)[0] == "session.start.rejected"


def test_stop_with_abnormal_reason_code() -> None:
    """异常收播记录原因码、说明并结束状态机。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )

    view = harness.manager.stop(
        stream_id,
        request=StopStreamRequest(
            reason=StopReason.UPSTREAM, detail="rtmp disconnected"
        ),
        operator="op_1",
    )

    assert view.status is SessionStatus.STOPPED
    assert view.state is SessionState.ENDED
    assert view.stop_reason is StopReason.UPSTREAM
    assert view.stop_detail == "rtmp disconnected"
    assert view.ended_at == NOW
    assert harness.control.snapshot(stream_id).state is (SessionState.ENDED)


def test_stop_before_start_is_rejected() -> None:
    """未开播不能收播。"""
    harness = Harness()
    stream_id = harness.create().stream_id

    with pytest.raises(ApiError) as excinfo:
        harness.manager.stop(
            stream_id,
            request=StopStreamRequest(reason=StopReason.OPERATOR),
            operator="op_1",
        )

    assert excinfo.value.code is ErrorCode.STATE_CONFLICT
    assert "session.stop.rejected" in harness.actions(stream_id)


def test_resume_rebuilds_runtime_and_clears_stop() -> None:
    """恢复开播重建状态机并可继续出队。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )
    harness.manager.stop(
        stream_id,
        request=StopStreamRequest(
            reason=StopReason.TIMEOUT, detail="upstream timeout"
        ),
        operator="op_1",
    )

    view = harness.manager.resume(
        stream_id,
        request=ResumeStreamRequest(reason="推流地址已更新"),
        operator="op_1",
    )

    assert view.status is SessionStatus.LIVE
    assert view.state is SessionState.IDLE
    assert view.stop_reason is None
    assert view.ended_at is None
    assert view.started_at == NOW
    queue = harness.control.snapshot(stream_id)
    assert queue.queue_depth == 0
    assert "session.resume" in harness.actions(stream_id)


def test_resume_before_stop_is_rejected() -> None:
    """未收播不能恢复。"""
    harness = Harness()
    stream_id = harness.create().stream_id

    with pytest.raises(ApiError) as excinfo:
        harness.manager.resume(
            stream_id,
            request=ResumeStreamRequest(),
            operator="op_1",
        )

    assert excinfo.value.code is ErrorCode.STATE_CONFLICT


def test_stop_during_takeover_closes_takeover() -> None:
    """接管期间收播会结束接管记录，恢复后回到空闲。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )
    harness.control.takeover(
        stream_id,
        action=TakeoverAction.TAKE,
        operator_id="op_2",
        reason="合规复核",
    )

    stopped = harness.manager.stop(
        stream_id,
        request=StopStreamRequest(
            reason=StopReason.COMPLIANCE, detail="直播间被封禁"
        ),
        operator="op_1",
    )

    assert stopped.state is SessionState.ENDED
    assert harness.control.snapshot(stream_id).takeover.active is False

    resumed = harness.manager.resume(
        stream_id,
        request=ResumeStreamRequest(reason="申诉通过"),
        operator="op_1",
    )

    assert resumed.state is SessionState.IDLE
    assert resumed.status is SessionStatus.LIVE


def test_invalid_config_and_operator_are_rejected() -> None:
    """配置非法与空操作人被拒绝。"""
    harness = Harness()

    with pytest.raises(ApiError) as excinfo:
        harness.manager.create(
            CreateStreamRequest(
                persona_id="p_1",
                persona_version=1,
                prompt_digest="digest",
                voice_id=" ",
                avatar_id="avatar_1",
            ),
            operator="op_1",
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(ApiError) as excinfo:
        harness.manager.create(
            CreateStreamRequest(
                persona_id="p_1",
                persona_version=1,
                prompt_digest="digest",
                voice_id="voice_1",
                avatar_id="avatar_1",
            ),
            operator=" ",
        )
    assert excinfo.value.code is ErrorCode.UNAUTHENTICATED


def test_unknown_stream_is_rejected() -> None:
    """未知场次的操作均返回 404。"""
    harness = Harness()

    with pytest.raises(ApiError) as excinfo:
        harness.manager.start(
            "st_missing",
            request=StartStreamRequest(),
            operator="op_1",
        )
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(ApiError) as excinfo:
        harness.manager.stop(
            "st_missing",
            request=StopStreamRequest(),
            operator="op_1",
        )
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_all_lifecycle_actions_are_audited() -> None:
    """创建、开播、收播、恢复全部写入审计。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )
    harness.manager.stop(
        stream_id,
        request=StopStreamRequest(
            reason=StopReason.OPERATOR, detail="正常下播"
        ),
        operator="op_1",
    )
    harness.manager.resume(
        stream_id,
        request=ResumeStreamRequest(reason="继续直播"),
        operator="op_1",
    )

    assert harness.actions(stream_id) == [
        "session.resume",
        "session.stop",
        "session.start",
        "session.create",
    ]
