"""平台接入与内部事件的核心模型。

字段与 docs/API.md 一一对应；对外契约的破坏性变更必须先升版本。
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 单条评论文本上限，超过视为异常数据而非正常评论
MAX_COMMENT_LENGTH = 500


class EventType(StrEnum):
    """首发仅支持评论创建事件。"""

    COMMENT_CREATED = "comment.created"


class StrictModel(BaseModel):
    """默认拒绝未知字段并不可变的基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CommentMessage(StrictModel):
    """规范化后的单条评论。

    Attributes:
        msg_id: 平台侧消息 ID，用于幂等去重。
        user_ref: 观众标识的哈希值，原始 ID 不得进入下游。
        text: 评论正文。
        nickname_ref: 昵称哈希，可为空。
    """

    msg_id: str = Field(min_length=1, max_length=128)
    user_ref: str = Field(min_length=8, max_length=128)
    text: str = Field(min_length=1, max_length=MAX_COMMENT_LENGTH)
    nickname_ref: str | None = Field(default=None, max_length=128)


class IngestEvent(StrictModel):
    """平台回调经适配层规范化后的事件。

    Attributes:
        schema_version: 事件结构版本，当前固定为 "1"。
        event_type: 事件类型。
        event_id: 事件唯一标识，用于幂等。
        occurred_at: 事件发生时间，必须带时区。
        room_ref: 平台直播间引用，用于限流与分区。
        message: 评论内容。
    """

    schema_version: Literal["1"] = "1"
    event_type: EventType
    event_id: str = Field(min_length=8, max_length=128)
    occurred_at: datetime
    room_ref: str = Field(min_length=1, max_length=128)
    message: CommentMessage

    @field_validator("occurred_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """拒绝不带时区的时间，避免跨时区误判。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value.astimezone(UTC)


class IngestResponse(StrictModel):
    """接入网关的幂等响应。

    Attributes:
        accepted: 是否已受理；重复事件同样返回 True。
        event_id: 对应的事件标识。
        trace_id: 服务端生成的追踪 ID。
    """

    accepted: bool = True
    event_id: str
    trace_id: str
