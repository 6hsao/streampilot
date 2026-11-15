"""统一错误码与错误响应。

错误码取值见 docs/API.md 第 1.3 节，新增错误码必须同步文档。
"""

from enum import StrEnum
from typing import Any

from pydantic import Field

from streampilot.contracts.models import StrictModel


class ErrorCode(StrEnum):
    """对外错误码。"""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    STATE_CONFLICT = "STATE_CONFLICT"
    ALREADY_TAKEOVER = "ALREADY_TAKEOVER"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    SAFETY_UNAVAILABLE = "SAFETY_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.STATE_CONFLICT: 409,
    ErrorCode.ALREADY_TAKEOVER: 409,
    ErrorCode.SAFETY_BLOCKED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.SAFETY_UNAVAILABLE: 503,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
}


def http_status_for(code: ErrorCode) -> int:
    """返回错误码对应的 HTTP 状态码。

    Args:
        code: 错误码。

    Returns:
        对应的 HTTP 状态码。
    """
    return _HTTP_STATUS[code]


class ErrorBody(StrictModel):
    """错误响应体。

    Attributes:
        code: 错误码。
        message: 面向调用方的英文说明，不得包含内部细节。
        retryable: 是否允许重试。
        trace_id: 追踪 ID。
        details: 结构化补充信息，可为空。
    """

    code: ErrorCode
    message: str
    retryable: bool = False
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiError(Exception):
    """携带错误码与 HTTP 状态的业务异常。

    Args:
        code: 错误码。
        message: 面向调用方的说明。
        retryable: 是否允许重试。
        details: 结构化补充信息。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    @property
    def http_status(self) -> int:
        """返回该异常对应的 HTTP 状态码。"""
        return http_status_for(self.code)


def error_body(
    code: ErrorCode,
    message: str,
    trace_id: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一错误响应体。

    Args:
        code: 错误码。
        message: 面向调用方的说明。
        trace_id: 追踪 ID。
        retryable: 是否允许重试。
        details: 结构化补充信息。

    Returns:
        可直接序列化为 JSON 的字典，外层键为 ``error``。
    """
    body = ErrorBody(
        code=code,
        message=message,
        retryable=retryable,
        trace_id=trace_id,
        details=details or {},
    )
    return {"error": body.model_dump(mode="json")}
