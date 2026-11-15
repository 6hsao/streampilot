"""LLM 网关的降级、限流、记账与 SSE 解析测试。"""

import json
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import pytest

from streampilot.llm import (
    AuthFailed,
    ChatMessage,
    DashScopeProvider,
    InMemoryTokenLedger,
    LLMGateway,
    LLMSettings,
    LLMUnavailable,
    RateLimited,
    StreamChunk,
    UpstreamTimeout,
    UpstreamUnavailable,
    Usage,
)
from streampilot.llm.errors import BadRequest
from streampilot.llm.provider import parse_chunk, sse_payloads

MESSAGES = (
    ChatMessage(role="system", content="你是主播小航"),
    ChatMessage(role="user", content="主播这个怎么买？"),
)


def _sse(*chunks: dict[str, Any]) -> str:
    """构造 SSE 响应体。"""
    lines = [
        f"data: {json.dumps(item, ensure_ascii=False)}" for item in chunks
    ]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def _delta(text: str) -> dict[str, Any]:
    """构造只含增量文本的分片。"""
    return {"choices": [{"delta": {"content": text}, "index": 0}]}


def _provider(
    handler: "httpx.MockTransport", api_key: str = "sk-test-key-123"
) -> DashScopeProvider:
    """基于 MockTransport 构造适配器。"""
    return DashScopeProvider(
        api_key,
        client=httpx.Client(transport=handler),
    )


class FakeProvider:
    """可编排失败与分片的假适配器。"""

    name = "fake"

    def __init__(self, behavior: dict[str, list[Any]]) -> None:
        self._behavior = behavior
        self.calls: list[str] = []

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """按预设脚本返回分片或抛出错误。"""
        self.calls.append(model)
        for item in self._behavior[model]:
            if isinstance(item, Exception):
                raise item
            yield item


def test_sse_payloads_stops_at_done() -> None:
    """解析器忽略非 data 行并在 [DONE] 结束。"""
    lines = ['data: {"a":1}', ": keep-alive", "data: [DONE]", "data: never"]

    assert list(sse_payloads(lines)) == ['{"a":1}']


def test_parse_chunk_extracts_text_and_usage() -> None:
    """分片解析出增量文本与用量。"""
    chunk = parse_chunk(
        json.dumps(
            {
                "choices": [
                    {"delta": {"content": "你好"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }
        )
    )

    assert chunk.delta == "你好"
    assert chunk.finish_reason == "stop"
    assert chunk.usage == Usage(tokens_in=12, tokens_out=3)


def test_dashscope_stream_reads_sse() -> None:
    """适配器把 SSE 转成流式分片。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=_sse(
                _delta("欢迎"),
                _delta("来到直播间"),
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6},
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = _provider(httpx.MockTransport(handler))
    chunks = list(
        provider.stream("qwen-plus", MESSAGES, max_tokens=50, temperature=0.5)
    )

    assert "".join(item.delta for item in chunks) == "欢迎来到直播间"
    assert chunks[-1].usage == Usage(tokens_in=20, tokens_out=6)
    assert seen["auth"] == "Bearer sk-test-key-123"
    assert seen["body"]["model"] == "qwen-plus"
    assert seen["body"]["stream"] is True


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthFailed),
        (429, RateLimited),
        (500, UpstreamUnavailable),
        (400, BadRequest),
    ],
)
def test_dashscope_maps_http_status(
    status: int, error: type[Exception]
) -> None:
    """HTTP 状态码映射为已分类错误。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    provider = _provider(transport)

    with pytest.raises(error):
        list(
            provider.stream(
                "qwen-plus", MESSAGES, max_tokens=10, temperature=0.5
            )
        )


def test_dashscope_maps_timeout() -> None:
    """读取超时映射为可重试超时错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = _provider(httpx.MockTransport(handler))

    with pytest.raises(UpstreamTimeout):
        list(
            provider.stream(
                "qwen-plus", MESSAGES, max_tokens=10, temperature=0.5
            )
        )


def test_provider_requires_api_key() -> None:
    """缺少 API Key 时启动即失败。"""
    with pytest.raises(ValueError):
        DashScopeProvider("   ")


def test_gateway_falls_back_on_rate_limit() -> None:
    """主模型限流时自动切到备选模型且不中断输出。"""
    provider = FakeProvider(
        {
            "qwen-plus": [RateLimited()],
            "qwen-turbo": [
                StreamChunk(delta="好的"),
                StreamChunk(
                    delta="，马上讲",
                    usage=Usage(tokens_in=10, tokens_out=4),
                ),
            ],
        }
    )
    ledger = InMemoryTokenLedger()
    gateway = LLMGateway(provider, ledger=ledger)

    chunks = list(gateway.stream(MESSAGES, session_id="ss_1"))

    assert "".join(item.delta for item in chunks) == "好的，马上讲"
    assert provider.calls == ["qwen-plus", "qwen-turbo"]
    assert gateway.stats.fallbacks == 1
    assert gateway.stats.served == 1
    assert ledger.by_session["ss_1"].usage == Usage(tokens_in=10, tokens_out=4)
    assert ledger.by_model["qwen-turbo"].requests == 1


def test_gateway_raises_unavailable_after_chain_exhausted() -> None:
    """降级链全部失败时抛出 LLMUnavailable，交由上层用预设话术。"""
    provider = FakeProvider(
        {"qwen-plus": [RateLimited()], "qwen-turbo": [UpstreamTimeout()]}
    )
    gateway = LLMGateway(provider)

    with pytest.raises(LLMUnavailable) as excinfo:
        list(gateway.stream(MESSAGES, session_id="ss_1"))

    assert excinfo.value.attempts == 2
    assert gateway.stats.failed == 1


def test_gateway_does_not_retry_non_retryable_error() -> None:
    """凭据错误不切换模型，直接失败。"""
    provider = FakeProvider({"qwen-plus": [AuthFailed()]})
    gateway = LLMGateway(provider)

    with pytest.raises(AuthFailed):
        list(gateway.stream(MESSAGES, session_id="ss_1"))

    assert provider.calls == ["qwen-plus"]
    assert gateway.stats.fallbacks == 0


def test_gateway_does_not_switch_model_after_output() -> None:
    """已产出内容后失败不重放，避免半句话被重复发声。"""
    provider = FakeProvider(
        {"qwen-plus": [StreamChunk(delta="半句"), RateLimited()]}
    )
    gateway = LLMGateway(provider)

    with pytest.raises(RateLimited):
        list(gateway.stream(MESSAGES, session_id="ss_1"))

    assert provider.calls == ["qwen-plus"]
    assert gateway.stats.failed == 1


def test_gateway_rejects_empty_chain() -> None:
    """空降级链直接报错。"""
    with pytest.raises(ValueError):
        LLMGateway(FakeProvider({}), chain=())


def test_settings_from_env() -> None:
    """配置从环境变量读取，缺失密钥拒绝启动。"""
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        LLMSettings.from_env({})
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        LLMSettings.from_env({"DASHSCOPE_API_KEY": "short"})

    settings = LLMSettings.from_env(
        {
            "DASHSCOPE_API_KEY": "sk-abcdefgh",
            "LLM_MODEL_CHAIN": "qwen-max, qwen-turbo",
            "LLM_MAX_TOKENS": "120",
        }
    )

    assert settings.model_chain == ("qwen-max", "qwen-turbo")
    assert settings.max_tokens == 120
    assert settings.temperature == 0.7
