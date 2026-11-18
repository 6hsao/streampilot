"""人工接管控制面的数据模型。

字段与 docs/API.md 第 4.2/4.3 节对应；状态取自
docs/ARCHITECTURE.md 第 5.1 节的场次状态机。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from streampilot.contracts.models import StrictModel
from streampilot.orchestrator.state import SessionState


class ControlAction(StrEnum):
    """运行控制动作。"""

    PAUSE = "pause"
    RESUME = "resume"
    MUTE = "mute"
    UNMUTE = "unmute"
    SWITCH_PERSONA = "switch_persona"
    SAY_TEMPLATE = "say_template"
    RECONNECT = "reconnect"


class TakeoverAction(StrEnum):
    """接管动作。"""

    TAKE = "take"
    RELEASE = "release"


class ControlRequest(StrictModel):
    """运行控制请求。

    Attributes:
        action: 控制动作。
        params: 动作参数，按动作类型取用。
        reason: 操作原因，写入审计。
    """

    action: ControlAction
    params: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=255)


class TakeoverRequest(StrictModel):
    """接管或释放请求。

    Attributes:
        action: 接管动作。
        reason: 操作原因；释放时同时作为处置结论。
    """

    action: TakeoverAction
    reason: str = Field(min_length=1, max_length=255)


class TakeoverInfo(StrictModel):
    """当前接管状态。

    Attributes:
        active: 是否处于人工接管。
        operator_id: 接管人标识。
        reason: 接管原因。
        started_at: 接管开始时间。
        conclusion: 释放时的处置结论。
    """

    active: bool = False
    operator_id: str | None = None
    reason: str | None = None
    started_at: datetime | None = None
    conclusion: str | None = None


class ControlResult(StrictModel):
    """运行控制结果。

    Attributes:
        stream_id: 场次标识。
        action: 执行的动作。
        state: 执行后的场次状态。
        applied: 是否已生效。
        ai_silent: AI 当前是否完全静默。
        latency_ms: 服务端处理耗时，用于核对接管时效。
    """

    stream_id: str
    action: ControlAction
    state: SessionState
    applied: bool = True
    ai_silent: bool
    latency_ms: float


class TakeoverResult(StrictModel):
    """接管或释放结果。

    Attributes:
        stream_id: 场次标识。
        action: 接管动作。
        state: 执行后的场次状态。
        takeover: 执行后的接管状态。
        cleared_queue: 本次清除的待发声数量。
        ai_silent: AI 当前是否完全静默。
        latency_ms: 服务端处理耗时，用于核对接管时效。
    """

    stream_id: str
    action: TakeoverAction
    state: SessionState
    takeover: TakeoverInfo
    cleared_queue: int = 0
    ai_silent: bool
    latency_ms: float


class ControlSnapshot(StrictModel):
    """控制面视角的场次快照。

    Attributes:
        stream_id: 场次标识。
        state: 场次状态。
        queue_depth: 待发声队列长度。
        paused: 是否暂停 AI 消费。
        muted: 是否静音。
        ai_silent: AI 当前是否完全静默。
        persona_version_id: 当前人设版本。
        templates: 可用预设话术标识。
        takeover: 接管状态。
    """

    stream_id: str
    state: SessionState
    queue_depth: int
    paused: bool
    muted: bool
    ai_silent: bool
    persona_version_id: str | None = None
    templates: list[str] = Field(default_factory=list)
    takeover: TakeoverInfo


class AuditEntry(StrictModel):
    """操作审计记录，字段对齐 audit_logs 表。

    Attributes:
        id: 记录标识。
        org_id: 组织标识。
        actor_id: 操作人标识。
        action: 动作名，拒绝的操作以 .rejected 结尾。
        target: 操作对象，控制面固定为场次标识。
        before: 操作前状态。
        after: 操作后状态或失败原因。
        created_at: 记录时间。
    """

    id: str
    org_id: str
    actor_id: str
    action: str
    target: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    created_at: datetime


class AuditPage(StrictModel):
    """审计记录列表。

    Attributes:
        entries: 按时间倒序排列的记录。
    """

    entries: list[AuditEntry] = Field(default_factory=list)
