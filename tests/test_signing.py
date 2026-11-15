"""接入签名校验的单元测试。"""

import pytest

from streampilot.ingest import (
    MAX_SKEW_SECONDS,
    InMemoryNonceStore,
    SignatureError,
    SignatureHeaders,
    canonical_string,
    sign,
    verify_request,
)

SECRET = b"unit-test-secret"
BODY = b'{"event_id":"evt_1"}'
NOW = 1_767_225_600.0


def _headers(
    *,
    body: bytes = BODY,
    timestamp: int | None = None,
    nonce: str = "nonce-1",
    secret: bytes = SECRET,
    method: str = "POST",
    path: str = "/v1/ingest/douyin/events",
) -> dict[str, str]:
    """构造一组合法签名请求头。"""
    ts = str(int(NOW) if timestamp is None else timestamp)
    canonical = canonical_string(method, path, ts, nonce, body)
    return {
        "X-Signature": sign(secret, canonical),
        "X-Timestamp": ts,
        "X-Nonce": nonce,
    }


def test_valid_signature_passes() -> None:
    """合法签名可通过校验。"""
    verify_request(
        SECRET,
        "POST",
        "/v1/ingest/douyin/events",
        _headers(),
        BODY,
        InMemoryNonceStore(),
        now=NOW,
    )


def test_canonical_string_format() -> None:
    """规范化原文顺序与分隔符符合约定。"""
    canonical = canonical_string(
        "post", "/v1/ingest", "1700000000", "n", b"abc"
    )

    assert canonical.split("\n")[:4] == [
        "POST",
        "/v1/ingest",
        "1700000000",
        "n",
    ]
    assert len(canonical.split("\n")[4]) == 64


def test_tampered_body_rejected() -> None:
    """请求体被修改后签名失效。"""
    with pytest.raises(SignatureError, match="signature verification failed"):
        verify_request(
            SECRET,
            "POST",
            "/v1/ingest/douyin/events",
            _headers(),
            b"tampered",
            InMemoryNonceStore(),
            now=NOW,
        )


def test_wrong_secret_rejected() -> None:
    """密钥不匹配时拒绝。"""
    headers = _headers(secret=b"other-secret")

    with pytest.raises(SignatureError) as excinfo:
        verify_request(
            SECRET,
            "POST",
            "/v1/ingest/douyin/events",
            headers,
            BODY,
            InMemoryNonceStore(),
            now=NOW,
        )
    assert excinfo.value.reason == "signature mismatch"


def test_stale_timestamp_rejected() -> None:
    """时间偏差超出窗口时拒绝。"""
    headers = _headers(timestamp=int(NOW) - MAX_SKEW_SECONDS - 1)

    with pytest.raises(SignatureError) as excinfo:
        verify_request(
            SECRET,
            "POST",
            "/v1/ingest/douyin/events",
            headers,
            BODY,
            InMemoryNonceStore(),
            now=NOW,
        )
    assert "window" in excinfo.value.reason


def test_invalid_timestamp_rejected() -> None:
    """非整数时间戳拒绝。"""
    headers = {
        "X-Signature": "00",
        "X-Timestamp": "not-a-number",
        "X-Nonce": "n",
    }

    with pytest.raises(SignatureError) as excinfo:
        verify_request(
            SECRET,
            "POST",
            "/v1/ingest",
            headers,
            BODY,
            InMemoryNonceStore(),
            now=NOW,
        )
    assert "integer" in excinfo.value.reason


def test_missing_header_rejected() -> None:
    """缺少签名头时拒绝并说明缺失字段。"""
    with pytest.raises(SignatureError) as excinfo:
        SignatureHeaders.from_mapping({"X-Signature": "abc"})

    assert "x-timestamp" in excinfo.value.reason


def test_nonce_replay_rejected() -> None:
    """同一 nonce 第二次请求被拒绝。"""
    store = InMemoryNonceStore()
    headers = _headers()

    verify_request(
        SECRET,
        "POST",
        "/v1/ingest/douyin/events",
        headers,
        BODY,
        store,
        now=NOW,
    )
    with pytest.raises(SignatureError) as excinfo:
        verify_request(
            SECRET,
            "POST",
            "/v1/ingest/douyin/events",
            headers,
            BODY,
            store,
            now=NOW,
        )
    assert "nonce" in excinfo.value.reason


def test_nonce_store_expires_entries() -> None:
    """过期 nonce 记录会被清理，可再次使用。"""
    store = InMemoryNonceStore()

    assert store.remember("n1", -1) is True
    assert store.remember("n1", 60) is True
    assert store.remember("n1", 60) is False
