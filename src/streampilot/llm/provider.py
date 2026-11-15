"""DashScope（OpenAI 兼容模式）流式适配器。

适配器可替换：网关只依赖 :class:`ChatProvider` 协议，新增供应商时
实现同样的 ``stream`` 即可，无需改动编排与安全逻辑。
"""

import json
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Protocol, cast

import httpx

from streampilot.llm.errors import (
    AuthFailed,
    BadRequest,
    RateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from streampilot.llm.types import ChatMessage, StreamChunk, Usage

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class ChatProvider(Protocol):
    """模型供应商适配器协议。"""

    name: str

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """发起流式对话。

        Args:
            model: 模型名称。
            messages: 对话消息。
            max_tokens: 输出 token 上限。
            temperature: 采样温度。

        Returns:
            流式分片迭代器。

        Raises:
            LLMError: 上游失败时抛出已分类错误。
        """
        ...


def sse_payloads(lines: Iterable[str]) -> Iterator[str]:
    """从 SSE 行中提取 data 负载。

    Args:
        lines: 原始行。

    Yields:
        非空 data 负载；遇到 ``[DONE]`` 结束。
    """
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        if payload:
            yield payload


def parse_chunk(payload: str) -> StreamChunk:
    """把一条 SSE data 负载解析为流式分片。

    Args:
        payload: JSON 文本。

    Returns:
        解析后的分片；无增量文本时 ``delta`` 为空串。
    """
    data = cast(dict[str, Any], json.loads(payload))
    delta = ""
    finish_reason: str | None = None
    choices = cast(list[dict[str, Any]], data.get("choices") or [])
    if choices:
        choice = choices[0]
        delta = str(
            cast(dict[str, Any], choice.get("delta") or {}).get("content")
            or ""
        )
        raw_finish = choice.get("finish_reason")
        finish_reason = str(raw_finish) if raw_finish else None
    usage: Usage | None = None
    raw_usage = data.get("usage")
    if isinstance(raw_usage, dict):
        usage = Usage(
            tokens_in=int(raw_usage.get("prompt_tokens") or 0),
            tokens_out=int(raw_usage.get("completion_tokens") or 0),
        )
    return StreamChunk(delta=delta, finish_reason=finish_reason, usage=usage)


class DashScopeProvider:
    """阿里云百炼的 OpenAI 兼容流式适配器。

    Args:
        api_key: 百炼 API Key，只从环境变量注入。
        base_url: 兼容模式基础地址。
        read_timeout_s: 单次读取超时，兼作首 token 与流内停顿上限。
        connect_timeout_s: 连接超时。
        client: 可选的外部 HTTP 客户端，便于测试与连接复用。

    Raises:
        ValueError: 未提供 API Key。
    """

    name = "dashscope"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DASHSCOPE_BASE_URL,
        read_timeout_s: float = 2.0,
        connect_timeout_s: float = 3.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DASHSCOPE_API_KEY is required")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                read_timeout_s, connect=connect_timeout_s, write=5.0
            )
        )

    def close(self) -> None:
        """关闭内部创建的 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        """把 HTTP 状态码映射为已分类错误。"""
        if status_code < 400:
            return
        if status_code == 401 or status_code == 403:
            raise AuthFailed()
        if status_code == 429:
            raise RateLimited()
        if status_code >= 500:
            raise UpstreamUnavailable()
        raise BadRequest()

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """按 SSE 流式读取增量文本与用量。"""
        payload = {
            "model": model,
            "messages": [
                {"role": item.role, "content": item.content}
                for item in messages
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as response:
                self._raise_for_status(response.status_code)
                for item in sse_payloads(response.iter_lines()):
                    yield parse_chunk(item)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout() from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable() from exc
