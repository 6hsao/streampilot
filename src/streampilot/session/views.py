"""场次管理的接口模型。"""

from datetime import datetime

from pydantic import Field

from streampilot.contracts.models import StrictModel
from streampilot.orchestrator.state import SessionState
from streampilot.session.models import SessionStatus, StopReason


class CreateStreamRequest(StrictModel):
    """创建场次请求。

    Attributes:
        persona_id: 人设标识。
        persona_version: 人设版本号。
        prompt_digest: 该版本提示词摘要，用于快照追溯。
        voice_id: 音色标识。
        avatar_id: 形象标识。
        title: 场次标题。
        platform_account_id: 平台账号标识。
        room_ref: 直播间引用。
    """

    persona_id: str = Field(min_length=1, max_length=64)
    persona_version: int = Field(ge=1)
    prompt_digest: str = Field(min_length=1, max_length=128)
    voice_id: str = Field(min_length=1, max_length=64)
    avatar_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=255)
    platform_account_id: str = Field(default="", max_length=64)
    room_ref: str = Field(default="", max_length=64)


class StartStreamRequest(StrictModel):
    """开播请求；人设与形象在创建时已固化。

    Attributes:
        expected_start_at: 期望开播时间，用于调度对齐。
    """

    expected_start_at: datetime | None = None


class StopStreamRequest(StrictModel):
    """收播请求。

    Attributes:
        reason: 原因码，异常收播必须有明确原因。
        detail: 具体说明。
    """

    reason: StopReason = StopReason.OPERATOR
    detail: str = Field(default="", max_length=255)


class ResumeStreamRequest(StrictModel):
    """恢复开播请求。

    Attributes:
        reason: 恢复原因，写入审计。
    """

    reason: str = Field(default="", max_length=255)


class SessionView(StrictModel):
    """场次视图，含开播时固化的配置快照。

    Attributes:
        stream_id: 推流场次标识。
        session_id: 会话标识。
        status: 场次状态。
        state: 编排状态机状态。
        title: 场次标题。
        persona_id: 人设标识。
        persona_version: 人设版本。
        prompt_digest: 提示词摘要。
        voice_id: 音色标识。
        avatar_id: 形象标识。
        platform_account_id: 平台账号标识。
        room_ref: 直播间引用。
        created_at: 创建时间。
        started_at: 首次开播时间。
        ended_at: 最近收播时间。
        stop_reason: 最近收播原因码。
        stop_detail: 最近收播说明。
    """

    stream_id: str
    session_id: str
    status: SessionStatus
    state: SessionState
    title: str = ""
    persona_id: str
    persona_version: int
    prompt_digest: str
    voice_id: str
    avatar_id: str
    platform_account_id: str = ""
    room_ref: str = ""
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    stop_reason: StopReason | None = None
    stop_detail: str = ""
