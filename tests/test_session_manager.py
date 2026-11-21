"""场次管理器的单元测试。"""

from datetime import UTC, datetime, timedelta
from typing import Any

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
from streampilot.session.facts import FactSource, StaticFactSource
from streampilot.session.manager import SessionManager
from streampilot.session.models import (
    SessionConfig,
    SessionStatus,
    StopReason,
)
from streampilot.session.permit import PermitRegistry, PushPermit
from streampilot.session.preflight import PreflightFacts, PreflightMode
from streampilot.session.views import (
    CreateStreamRequest,
    ResumeStreamRequest,
    SessionView,
    StartStreamRequest,
    StopStreamRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _permit_registry() -> PermitRegistry:
    """构造含一份有效许可的登记表。"""
    registry = PermitRegistry()
    registry.register(
        PushPermit(
            number="permit_001",
            expires_at=NOW + timedelta(days=30),
            egress_ips=("203.0.113.10",),
            archive_ref="GOV-05/2026-001",
        )
    )
    return registry


def _passing_source(**overrides: Any) -> StaticFactSource:
    """构造全部通过的事实源。"""
    base: dict[str, Any] = {
        "platform_authorized_until": NOW + timedelta(days=30),
        "avatar_license_until": NOW + timedelta(days=30),
        "voice_license_until": NOW + timedelta(days=30),
        "aigc_label_enabled": True,
        "safety_probe": lambda: True,
        "rtmp_credential_probe": lambda: True,
        "rtmp_reachable_probe": lambda: True,
        "live2d_model_ready": True,
        "cubism_license_recorded": True,
        "permits": _permit_registry(),
    }
    base.update(overrides)
    return StaticFactSource(**base)


class ToggleFacts:
    """可切换通过/失败的事实源，用于验证恢复前复检。"""

    def __init__(self) -> None:
        self.ok = True
        self._source = _passing_source(
            safety_probe=lambda: self.ok,
            rtmp_credential_probe=lambda: self.ok,
        )

    def collect(self, stream_id: str, config: SessionConfig) -> PreflightFacts:
        """返回当前模式下的自检事实。"""
        return self._source.collect(stream_id, config)


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

    def __init__(self, facts: FactSource | None = None) -> None:
        self.audit = InMemoryAuditStore()
        self.control = ControlService(
            audit=self.audit,
            takeovers=InMemoryTakeoverStore(),
            clock=lambda: NOW,
        )
        self.sinks: dict[str, FakeSink] = {}
        self.toggle = ToggleFacts()
        self.manager = SessionManager(
            control=self.control,
            audit=self.audit,
            clock=lambda: NOW,
            runtime_factory=self._factory,
            facts=facts or self.toggle,
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


def test_start_requires_preflight_pass() -> None:
    """自检失败阻断开播并返回原因，修复后可再次开播。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.toggle.ok = False

    with pytest.raises(ApiError) as excinfo:
        harness.manager.start(
            stream_id,
            request=StartStreamRequest(),
            operator="op_1",
        )

    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
    failed = excinfo.value.details["failed_checks"]
    assert {"safety_health", "rtmp_credential"} <= set(failed)
    assert "session.start.rejected" in harness.actions(stream_id)
    assert harness.control.snapshot(stream_id).state is SessionState.IDLE

    harness.toggle.ok = True
    view = harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )

    assert view.status is SessionStatus.LIVE


def test_start_requires_push_permit() -> None:
    """未登记推流许可时开播被拒并给出原因码。"""
    harness = Harness(
        facts=_passing_source(permits=PermitRegistry()),
    )
    stream_id = harness.create().stream_id

    with pytest.raises(ApiError) as excinfo:
        harness.manager.start(
            stream_id,
            request=StartStreamRequest(),
            operator="op_1",
        )

    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
    assert "push_permit" in excinfo.value.details["failed_checks"]


def test_start_in_local_mode_without_permit() -> None:
    """本地伴侣模式无需云端许可即可开播。"""
    source = StaticFactSource(
        mode=PreflightMode.LOCAL,
        aigc_label_enabled=True,
        safety_probe=lambda: True,
        local_broadcast_ready=True,
    )
    harness = Harness(facts=source)
    stream_id = harness.create().stream_id

    view = harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )

    assert view.status is SessionStatus.LIVE
    entries = {item.action: item for item in harness.audit.entries(stream_id)}
    after = entries["session.start"].after
    assert after is not None
    assert after["mode"] == "local"
    assert after["permit_number"] == ""


def test_resume_rechecks_preflight() -> None:
    """恢复开播重新自检，未通过保持收播状态。"""
    harness = Harness()
    stream_id = harness.create().stream_id
    harness.manager.start(
        stream_id,
        request=StartStreamRequest(),
        operator="op_1",
    )
    harness.manager.stop(
        stream_id,
        request=StopStreamRequest(reason=StopReason.OPERATOR),
        operator="op_1",
    )
    harness.toggle.ok = False

    with pytest.raises(ApiError) as excinfo:
        harness.manager.resume(
            stream_id,
            request=ResumeStreamRequest(reason="继续直播"),
            operator="op_1",
        )

    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
    assert "session.resume.rejected" in harness.actions(stream_id)

    harness.toggle.ok = True
    view = harness.manager.resume(
        stream_id,
        request=ResumeStreamRequest(reason="继续直播"),
        operator="op_1",
    )

    assert view.status is SessionStatus.LIVE


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
