"""公共接口契约。

对外暴露事件模型、错误码与错误响应构造器，服务不得私自定义同名字段。
"""

from streampilot.contracts.errors import (
    ApiError,
    ErrorBody,
    ErrorCode,
    error_body,
    http_status_for,
)
from streampilot.contracts.models import (
    CommentMessage,
    EventType,
    IngestEvent,
    IngestResponse,
)

__all__ = [
    "ApiError",
    "CommentMessage",
    "ErrorBody",
    "ErrorCode",
    "EventType",
    "IngestEvent",
    "IngestResponse",
    "error_body",
    "http_status_for",
]
