"""接入网关接口的契约测试。"""

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from streampilot.ingest.app import (
    INGEST_PATH,
    IngestSettings,
    create_app,
)
from streampilot.ingest.signing import (
    InMemoryNonceStore,
    canonical_string,
    sign,
)
from streampilot.ingest.stores import (
    InMemoryEventPublisher,
    InMemoryIdempotencyStore,
    InMemoryRateLimiter,
)

SECRET = b"unit-test-secret-0123456789"
TRACE_HEADER = "x-trace-id"


def _payload(
    event_id: str = "evt_01J8ZQ7K3M9WQ2V6H4T8R1X5B0",
    msg_id: str = "msg_987654321",
) -> dict[str, Any]:
    """构造一条合法事件载荷。"""
    return {
        "event_type": "comment.created",
        "event_id": event_id,
        "occurred_at": "2026-01-01T12:00:00Z",
        "room_ref": "room_123456",
        "message": {
            "msg_id": msg_id,
            "user_ref": "hashed_user_abc",
            "text": "主播这个怎么买？",
        },
    }


def _client(
    *,
    limit: int = 10,
) -> tuple[TestClient, InMemoryEventPublisher]:
    """构建带内存依赖的测试客户端。"""
    publisher = InMemoryEventPublisher()
    settings = IngestSettings(
        signing_secret=SECRET,
        rate_limit_per_second=limit,
    )
    app = create_app(
        settings,
        nonce_store=InMemoryNonceStore(),
        idempotency=InMemoryIdempotencyStore(),
        rate_limiter=InMemoryRateLimiter(limit),
        publisher=publisher,
    )
    return TestClient(app), publisher


def _post(
    client: TestClient,
    body: Any,
    *,
    nonce: str = "nonce-1",
    secret: bytes = SECRET,
    timestamp: str | None = None,
    trace_id: str | None = None,
) -> Any:
    """按契约签名并提交事件。"""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ts = timestamp or str(int(time.time()))
    canonical = canonical_string("POST", INGEST_PATH, ts, nonce, raw)
    headers = {
        "X-Signature": sign(secret, canonical),
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "Content-Type": "application/json",
    }
    if trace_id:
        headers[TRACE_HEADER] = trace_id
    return client.post(INGEST_PATH, content=raw, headers=headers)


def test_valid_event_is_accepted_and_published() -> None:
    """合法事件返回 202 且只投递一次。"""
    client, publisher = _client()

    response = _post(client, _payload(), trace_id="trace-abc")

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "event_id": "evt_01J8ZQ7K3M9WQ2V6H4T8R1X5B0",
        "trace_id": "trace-abc",
    }
    assert len(publisher.published) == 1
    assert publisher.published[0][1] == "trace-abc"


def test_invalid_signature_is_rejected() -> None:
    """签名错误返回 401 且不投递。"""
    client, publisher = _client()

    response = _post(client, _payload(), secret=b"wrong-secret")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert publisher.published == []


def test_duplicate_event_is_idempotent() -> None:
    """重复 event_id 返回成功但不重复投递。"""
    client, publisher = _client()

    first = _post(client, _payload(), nonce="n1")
    second = _post(client, _payload(), nonce="n2")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["accepted"] is True
    assert len(publisher.published) == 1


def test_duplicate_msg_id_is_deduplicated() -> None:
    """同一 msg_id 的不同事件只投递一次。"""
    client, publisher = _client()
    first = _payload("evt_first_0001")
    second = _payload("evt_second_0002")

    first_response = _post(client, first, nonce="n1")
    second_response = _post(client, second, nonce="n2")

    assert first_response.status_code == 202
    assert second_response.status_code == 202
    assert len(publisher.published) == 1


def test_rate_limit_returns_429_with_retry_after() -> None:
    """超出房间限额返回 429 并带 Retry-After。"""
    client, publisher = _client(limit=1)

    first = _post(client, _payload("evt_first_0001", "msg_first"), nonce="n1")
    second = _post(
        client, _payload("evt_second_0002", "msg_second"), nonce="n2"
    )

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert len(publisher.published) == 1


def test_invalid_payload_returns_400() -> None:
    """非法载荷返回 INVALID_ARGUMENT。"""
    client, _ = _client()

    response = _post(client, {"event_type": "comment.created"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_healthz() -> None:
    """存活探针返回 ok。"""
    client, _ = _client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_require_strong_secret() -> None:
    """缺失或过短的密钥直接拒绝启动。"""
    with pytest.raises(RuntimeError, match="INGEST_CALLBACK_SECRET"):
        IngestSettings.from_env({})
    with pytest.raises(RuntimeError, match="INGEST_CALLBACK_SECRET"):
        IngestSettings.from_env({"INGEST_CALLBACK_SECRET": "short"})

    settings = IngestSettings.from_env(
        {
            "INGEST_CALLBACK_SECRET": "0123456789abcdef",
            "INGEST_RATE_LIMIT_PER_SECOND": "80",
        }
    )

    assert settings.signing_secret == b"0123456789abcdef"
    assert settings.rate_limit_per_second == 80
    assert settings.idempotency_ttl_seconds == 3600
