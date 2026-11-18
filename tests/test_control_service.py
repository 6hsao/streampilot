"""人工接管控制服务的单元测试。"""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from streampilot.contracts import ApiError, ErrorCode
from streampilot.control.models import (
    ControlAction,
    ControlRequest,
    TakeoverAction,
)
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.state import SessionState, SessionStateMachine

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FakeSink:
    """记录控制指令的假下游。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def silence(self) -> None:
        """记录静默。"""
        self.calls.append(("silence", None))

    def resume(self) -> None:
        """记录恢复。"""
        self.calls.append(("resume", None))

    def set_muted(self, muted: bool) -> None:
        """记录静音切换。"""
        self.calls.append(("set_muted", muted))

    def switch_persona(self, persona_version_id: str) -> None:
        """记录人设切换。"""
        self.calls.append(("switch_persona", persona_version_id))

    def speak_template(self, template_id: str, text: str) -> None:
        """记录话术播放。"""
        self.calls.append(("speak_template", template_id))

    def reconnect_stream(self) -> None:
        """记录重连。"""
        self.calls.append(("reconnect_stream", None))


@dataclass
class Harness:
    """测试装置：服务、场次运行时与审计存储。

    Attributes:
        service: 控制服务。
        control: 场次运行时。
        queue: 具体队列类型，便于测试构造发言。
        sink: 假下游。
        audit: 审计存储。
    """

    service: ControlService
    control: SessionControl
    queue: InMemorySpeechQueue
    sink: FakeSink
    audit: InMemoryAuditStore


def make_harness(
    state: SessionState = SessionState.IDLE,
) -> Harness:
    """构建单场次测试装置。"""
    audit = InMemoryAuditStore()
    sink = FakeSink()
    service = ControlService(
        audit=audit,
        takeovers=InMemoryTakeoverStore(),
        clock=lambda: NOW,
    )
    queue = InMemorySpeechQueue(cooldown_ms=0)
    control = SessionControl(
        stream_id="st_1",
        state=SessionStateMachine(state=state),
        queue=queue,
        sink=sink,
        templates={"welcome": "欢迎来到直播间"},
    )
    service.register(control)
    return Harness(service, control, queue, sink, audit)


def _control(
    harness: Harness,
    action: ControlAction,
    params: dict[str, str] | None = None,
) -> None:
    """执行一次控制动作。"""
    harness.service.control(
        "st_1",
        request=ControlRequest(action=action, params=params or {}),
        operator_id="op_1",
    )


def _audit_actions(harness: Harness) -> list[str]:
    """返回审计动作名，时间倒序。"""
    return [entry.action for entry in harness.audit.entries("st_1")]


def test_takeover_clears_queue_and_silences() -> None:
    """接管后清空待发声并立即静默。"""
    harness = make_harness(SessionState.SPEAKING)
    queue = harness.queue
    queue.enqueue(queue.make_turn("c_1", "第一条", 1_000))
    queue.enqueue(queue.make_turn("c_2", "第二条", 1_000))

    result = harness.service.takeover(
        "st_1",
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="敏感话题",
    )

    assert result.state is SessionState.TAKEOVER
    assert result.cleared_queue == 2
    assert result.takeover.active is True
    assert result.takeover.operator_id == "op_1"
    assert result.ai_silent is True
    assert result.latency_ms < 3_000
    assert queue.stats().size == 0
    assert ("silence", None) in harness.sink.calls
    assert harness.service.dequeue("st_1", 1_000) is None
    assert _audit_actions(harness) == ["takeover.take"]


def test_ai_stays_silent_after_late_enqueue() -> None:
    """接管期间新入队的发言也不得出声。"""
    harness = make_harness()
    harness.service.takeover(
        "st_1",
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="人工处置",
    )
    queue = harness.queue
    queue.enqueue(queue.make_turn("c_late", "后来的一条", 1_000))

    assert harness.service.is_ai_silent("st_1") is True
    assert harness.service.dequeue("st_1", 1_000) is None
    assert queue.stats().size == 1


def test_release_restores_ai_with_conclusion() -> None:
    """释放接管写入处置结论并恢复 AI。"""
    harness = make_harness()
    harness.service.takeover(
        "st_1",
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="人工处置",
    )
    queue = harness.queue
    queue.enqueue(queue.make_turn("c_stale", "接管期间的回复", 1_000))

    result = harness.service.takeover(
        "st_1",
        action=TakeoverAction.RELEASE,
        operator_id="op_1",
        reason="敏感话题已核实并处理",
    )

    assert result.state is SessionState.IDLE
    assert result.cleared_queue == 1
    assert result.takeover.active is False
    assert result.takeover.conclusion == "敏感话题已核实并处理"
    assert result.ai_silent is False
    assert ("resume", None) in harness.sink.calls

    queue.enqueue(queue.make_turn("c_new", "新的评论", 2_000))
    turn = harness.service.dequeue("st_1", 2_000)
    assert turn is not None
    assert turn.item_id == "c_new"
    assert _audit_actions(harness) == [
        "takeover.release",
        "takeover.take",
    ]

    with pytest.raises(ApiError) as excinfo:
        harness.service.takeover(
            "st_1",
            action=TakeoverAction.RELEASE,
            operator_id="op_1",
            reason="重复释放",
        )
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT


def test_duplicate_takeover_is_rejected() -> None:
    """重复接管返回 ALREADY_TAKEOVER 并留痕。"""
    harness = make_harness()
    harness.service.takeover(
        "st_1",
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="第一次",
    )

    with pytest.raises(ApiError) as excinfo:
        harness.service.takeover(
            "st_1",
            action=TakeoverAction.TAKE,
            operator_id="op_2",
            reason="第二次",
        )

    assert excinfo.value.code is ErrorCode.ALREADY_TAKEOVER
    assert _audit_actions(harness)[0] == "takeover.take.rejected"


def test_ended_stream_rejects_control_and_takeover() -> None:
    """收播后拒绝接管与控制。"""
    harness = make_harness()
    harness.control.state.end()

    with pytest.raises(ApiError) as excinfo:
        harness.service.takeover(
            "st_1",
            action=TakeoverAction.TAKE,
            operator_id="op_1",
            reason="收播后",
        )
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT

    with pytest.raises(ApiError) as excinfo:
        _control(harness, ControlAction.PAUSE)
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT


def test_release_after_circuit_returns_to_circuit() -> None:
    """接管前处于熔断时，释放后回到熔断且保持静默。"""
    harness = make_harness()
    harness.control.state.trip("safety-unavailable")
    harness.service.takeover(
        "st_1",
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="熔断期间接管",
    )

    result = harness.service.takeover(
        "st_1",
        action=TakeoverAction.RELEASE,
        operator_id="op_1",
        reason="熔断原因已确认",
    )

    assert result.state is SessionState.CIRCUIT_OPEN
    assert result.ai_silent is True
    assert ("resume", None) not in harness.sink.calls
    assert harness.service.dequeue("st_1", 1_000) is None


def test_pause_keeps_queue_but_stops_speech() -> None:
    """暂停保留队列但不出声，恢复后继续。"""
    harness = make_harness()
    queue = harness.queue
    queue.enqueue(queue.make_turn("c_1", "等一等", 1_000))

    _control(harness, ControlAction.PAUSE)

    assert harness.service.is_ai_silent("st_1") is True
    assert queue.stats().size == 1
    assert harness.service.dequeue("st_1", 1_000) is None

    _control(harness, ControlAction.RESUME)

    assert harness.service.is_ai_silent("st_1") is False
    turn = harness.service.dequeue("st_1", 1_000)
    assert turn is not None
    assert turn.item_id == "c_1"


def test_mute_blocks_ai_and_template_until_unmute() -> None:
    """静音时 AI 与预设话术都不出声。"""
    harness = make_harness()
    _control(harness, ControlAction.MUTE)

    assert ("set_muted", True) in harness.sink.calls
    assert harness.service.is_ai_silent("st_1") is True

    with pytest.raises(ApiError) as excinfo:
        _control(
            harness,
            ControlAction.SAY_TEMPLATE,
            {"template_id": "welcome"},
        )
    assert excinfo.value.code is ErrorCode.STATE_CONFLICT

    _control(harness, ControlAction.UNMUTE)
    _control(harness, ControlAction.SAY_TEMPLATE, {"template_id": "welcome"})

    assert ("speak_template", "welcome") in harness.sink.calls
    assert harness.service.is_ai_silent("st_1") is False


def test_control_actions_are_all_audited() -> None:
    """每个成功动作都写入审计。"""
    harness = make_harness()

    _control(harness, ControlAction.PAUSE)
    _control(harness, ControlAction.RESUME)
    _control(harness, ControlAction.MUTE)
    _control(harness, ControlAction.UNMUTE)
    _control(
        harness,
        ControlAction.SWITCH_PERSONA,
        {"persona_version_id": "pv_2"},
    )
    _control(harness, ControlAction.SAY_TEMPLATE, {"template_id": "welcome"})
    _control(harness, ControlAction.RECONNECT)

    assert harness.service.snapshot("st_1").persona_version_id == "pv_2"
    assert ("switch_persona", "pv_2") in harness.sink.calls
    assert ("reconnect_stream", None) in harness.sink.calls
    assert _audit_actions(harness) == [
        "control.reconnect",
        "control.say_template",
        "control.switch_persona",
        "control.unmute",
        "control.mute",
        "control.resume",
        "control.pause",
    ]


def test_invalid_parameters_are_rejected_and_audited() -> None:
    """缺少参数或话术不存在时拒绝并留痕。"""
    harness = make_harness()

    with pytest.raises(ApiError) as excinfo:
        _control(harness, ControlAction.SWITCH_PERSONA)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(ApiError) as excinfo:
        _control(
            harness,
            ControlAction.SAY_TEMPLATE,
            {"template_id": "missing"},
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    rejected = [
        entry
        for entry in harness.audit.entries("st_1")
        if entry.action.endswith(".rejected")
    ]
    assert len(rejected) == 2
    assert rejected[0].after == {
        "error": "INVALID_ARGUMENT",
    }


def test_unknown_stream_and_operator_are_rejected() -> None:
    """未知场次返回 404，空操作人返回 401。"""
    harness = make_harness()

    with pytest.raises(ApiError) as excinfo:
        harness.service.snapshot("st_missing")
    assert excinfo.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(ApiError) as excinfo:
        harness.service.control(
            "st_1",
            request=ControlRequest(action=ControlAction.PAUSE),
            operator_id=" ",
        )
    assert excinfo.value.code is ErrorCode.UNAUTHENTICATED


def test_audit_limit_is_validated() -> None:
    """审计查询条数越界直接拒绝。"""
    harness = make_harness()

    with pytest.raises(ApiError) as excinfo:
        harness.service.audit("st_1", 0)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    harness.service.audit("st_1", 10)


def test_takeover_latency_p95_within_budget() -> None:
    """接管生效 p95 应低于 3 秒（进程内应远低于预算）。"""
    harness = make_harness()
    latencies: list[float] = []

    for _ in range(20):
        result = harness.service.takeover(
            "st_1",
            action=TakeoverAction.TAKE,
            operator_id="op_1",
            reason="压测",
        )
        latencies.append(result.latency_ms)
        harness.service.takeover(
            "st_1",
            action=TakeoverAction.RELEASE,
            operator_id="op_1",
            reason="压测结束",
        )

    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
    assert p95 < 3_000
