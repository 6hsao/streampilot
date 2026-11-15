"""公共契约模型的单元测试。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from streampilot.contracts import (
    CommentMessage,
    ErrorCode,
    EventType,
    IngestEvent,
    IngestResponse,
    error_body,
    http_status_for,
)
from streampilot.contracts.errors import ApiError


def _event(**overrides: object) -> IngestEvent:
    """构造一条合法事件，供各用例按需覆盖字段。"""
    payload: dict[str, object] = {
        "event_type": EventType.COMMENT_CREATED,
        "event_id": "evt_01J8ZQ7K3M9WQ2V6H4T8R1X5B0",
        "occurred_at": "2026-01-01T12:00:00Z",
        "room_ref": "room_123456",
        "message": {
            "msg_id": "msg_987654321",
            "user_ref": "hashed_user_abc",
            "text": "主播这个怎么买？",
        },
    }
    payload.update(overrides)
    return IngestEvent.model_validate(payload)


def test_valid_event_is_frozen_and_normalized() -> None:
    """合法事件可解析，且时间被归一化为 UTC。"""
    event = _event()

    assert event.event_type is EventType.COMMENT_CREATED
    assert event.occurred_at == datetime(2026, 1, 1, 12, tzinfo=UTC)
    with pytest.raises(ValidationError):
        setattr(event, "room_ref", "room_other")  # noqa: B010


def test_naive_timestamp_is_rejected() -> None:
    """不带时区的时间必须被拒绝。"""
    with pytest.raises(ValidationError):
        _event(occurred_at="2026-01-01T12:00:00")


def test_unknown_field_is_rejected() -> None:
    """未知字段默认拒绝，避免平台字段静默漂移。"""
    with pytest.raises(ValidationError):
        _event(unknown_field="x")


def test_comment_text_length_limit() -> None:
    """超过长度上限的评论被拒绝。"""
    with pytest.raises(ValidationError):
        CommentMessage(
            msg_id="msg_1",
            user_ref="hashed_user_abc",
            text="a" * 501,
        )


def test_ingest_response_defaults() -> None:
    """受理响应默认 accepted 为 True。"""
    resp = IngestResponse(event_id="evt_1", trace_id="trace_1")

    assert resp.accepted is True
    assert resp.model_dump() == {
        "accepted": True,
        "event_id": "evt_1",
        "trace_id": "trace_1",
    }


def test_error_body_envelope() -> None:
    """错误响应外层固定为 error，且包含追踪 ID。"""
    body = error_body(
        ErrorCode.SAFETY_BLOCKED,
        "blocked by policy",
        "trace_1",
        details={"stage": "input"},
    )

    assert body["error"]["code"] == "SAFETY_BLOCKED"
    assert body["error"]["retryable"] is False
    assert body["error"]["trace_id"] == "trace_1"
    assert body["error"]["details"] == {"stage": "input"}


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.INVALID_ARGUMENT, 400),
        (ErrorCode.UNAUTHENTICATED, 401),
        (ErrorCode.PERMISSION_DENIED, 403),
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.STATE_CONFLICT, 409),
        (ErrorCode.ALREADY_TAKEOVER, 409),
        (ErrorCode.SAFETY_BLOCKED, 422),
        (ErrorCode.RATE_LIMITED, 429),
        (ErrorCode.SAFETY_UNAVAILABLE, 503),
        (ErrorCode.UPSTREAM_TIMEOUT, 504),
    ],
)
def test_http_status_mapping(code: ErrorCode, status: int) -> None:
    """错误码与 HTTP 状态码的映射与契约一致。"""
    assert http_status_for(code) == status


def test_api_error_exposes_status() -> None:
    """业务异常可直接取到 HTTP 状态码。"""
    exc = ApiError(ErrorCode.RATE_LIMITED, "too many requests", retryable=True)

    assert exc.http_status == 429
    assert exc.retryable is True
    assert exc.details == {}
