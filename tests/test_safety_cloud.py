"""云审核适配器、签名与配额的测试。"""

import hashlib
import json
from typing import Any

import httpx
import pytest

from streampilot.safety import (
    AliyunGreenModerator,
    MinuteQuota,
    ModerationUnavailableError,
    parse_verdict,
    sign_request,
)


class FakeClock:
    """可推进的时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_minute_quota_resets_each_window() -> None:
    """配额按分钟窗口重置。"""
    clock = FakeClock(0.0)
    quota = MinuteQuota(2, clock)

    assert quota.try_acquire()
    assert quota.try_acquire()
    assert not quota.try_acquire()

    clock.advance(60.0)

    assert quota.try_acquire()


def test_parse_verdict_blocks_risk_labels() -> None:
    """命中风险标签即拦截。"""
    verdict = parse_verdict(
        {"Data": {"Labels": "politics,ad"}},
        provider="aliyun_green",
        latency_ms=42,
    )

    assert verdict.status == "block"
    assert verdict.labels == ("politics", "ad")
    assert verdict.latency_ms == 42


def test_parse_verdict_accepts_enhanced_lowercase_labels() -> None:
    """增强版实际返回小写 labels 时同样拦截。"""
    verdict = parse_verdict(
        {
            "RequestId": "req-1",
            "Message": "OK",
            "Code": 200,
            "Data": {
                "labels": "contraband,ad",
                "descriptions": "疑似违禁内容,疑似广告内容",
            },
        },
        provider="aliyun_green",
        latency_ms=139,
    )

    assert verdict.status == "block"
    assert verdict.labels == ("contraband", "ad")


def test_parse_verdict_passes_clean_text() -> None:
    """仅 nonLabel 或空 labels 时放行。"""
    verdict = parse_verdict(
        {"Data": {"Result": [{"Label": "nonLabel", "Confidence": 99}]}},
        provider="aliyun_green",
        latency_ms=5,
    )
    empty = parse_verdict(
        {"Code": 200, "Data": {"labels": ""}},
        provider="aliyun_green",
        latency_ms=5,
    )

    assert verdict.status == "pass"
    assert verdict.labels == ()
    assert empty.status == "pass"


def test_parse_verdict_rejects_business_error_code() -> None:
    """业务码非 200 按不可用处理。"""
    with pytest.raises(ModerationUnavailableError, match="code 400"):
        parse_verdict(
            {"Code": 400, "Data": {"labels": ""}},
            provider="aliyun_green",
            latency_ms=1,
        )


def test_parse_verdict_rejects_unknown_shape() -> None:
    """响应结构异常按不可用处理，不能当作放行。"""
    with pytest.raises(ModerationUnavailableError):
        parse_verdict({"Code": 500}, provider="aliyun_green", latency_ms=1)


def test_sign_request_produces_acs3_headers() -> None:
    """签名结果包含 ACS3 头且内容哈希与请求体一致。"""
    body = b'{"Service":"comment_detection"}'

    signed = sign_request(
        method="POST",
        url="https://green-cip.cn-hangzhou.aliyuncs.com/",
        body=body,
        access_key_id="ak-test",
        access_key_secret="secret-test",
        timestamp="2026-01-01T00:00:00Z",
        nonce="fixed-nonce",
    )

    assert signed.body == body
    assert signed.headers["x-acs-action"] == "TextModeration"
    assert (
        signed.headers["x-acs-content-sha256"]
        == hashlib.sha256(body).hexdigest()
    )
    assert signed.headers["host"] == "green-cip.cn-hangzhou.aliyuncs.com"
    auth = signed.headers["Authorization"]
    assert auth.startswith(
        "ACS3-HMAC-SHA256 Credential=ak-test,SignedHeaders="
    )
    signature = auth.rsplit("Signature=", 1)[1]
    assert len(signature) == 64
    int(signature, 16)


def test_sign_request_is_deterministic_for_fixed_inputs() -> None:
    """相同输入得到相同签名。"""
    kwargs: dict[str, Any] = {
        "method": "POST",
        "url": "https://example.com/",
        "body": b"{}",
        "access_key_id": "ak",
        "access_key_secret": "sk",
        "timestamp": "2026-01-01T00:00:00Z",
        "nonce": "n",
    }

    assert sign_request(**kwargs) == sign_request(**kwargs)


def _moderator(handler: httpx.MockTransport) -> AliyunGreenModerator:
    """基于 MockTransport 构造适配器。"""
    return AliyunGreenModerator(
        "ak-test",
        "secret-test",
        client=httpx.Client(transport=handler),
        nonce_factory=lambda: "fixed",
    )


def test_moderator_reports_risk_labels() -> None:
    """正常响应被映射为拦截结论。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"Data": {"Labels": "politics", "Reason": "hit"}}
        )

    moderator = _moderator(httpx.MockTransport(handler))

    verdict = moderator.moderate("违规内容", "input")

    assert verdict.status == "block"
    assert verdict.labels == ("politics",)
    assert seen["url"] == "https://green-cip.cn-hangzhou.aliyuncs.com/"
    assert seen["auth"].startswith("ACS3-HMAC-SHA256 ")
    assert seen["body"]["Service"] == "comment_detection"
    assert "违规内容" in seen["body"]["ServiceParameters"]


def test_moderator_maps_timeout_to_unavailable() -> None:
    """读取超时按不可用处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    moderator = _moderator(httpx.MockTransport(handler))

    with pytest.raises(ModerationUnavailableError):
        moderator.moderate("文本", "output")


def test_moderator_rejects_bad_status_and_body() -> None:
    """非 200 或非 JSON 响应都不能被当作放行。"""
    bad_status = _moderator(
        httpx.MockTransport(lambda request: httpx.Response(503))
    )
    with pytest.raises(ModerationUnavailableError):
        bad_status.moderate("文本", "input")

    bad_body = _moderator(
        httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>oops</html>")
        )
    )
    with pytest.raises(ModerationUnavailableError):
        bad_body.moderate("文本", "input")


def test_moderator_requires_credentials() -> None:
    """缺少凭据时启动即失败。"""
    with pytest.raises(ValueError):
        AliyunGreenModerator("", "secret")
    with pytest.raises(ValueError):
        AliyunGreenModerator("ak", "  ")
