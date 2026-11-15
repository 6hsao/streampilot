"""LLM 薄网关：模型降级链、流式转发与 token 记账。

不做提示词工程与内容安全，只负责「把一次生成可靠地跑完」。
"""

from collections.abc import Iterator, Sequence

from streampilot.llm.errors import LLMError, LLMUnavailable
from streampilot.llm.ledger import InMemoryTokenLedger, TokenLedger
from streampilot.llm.provider import ChatProvider
from streampilot.llm.types import (
    ChatMessage,
    GatewayStats,
    StreamChunk,
    Usage,
)

# 主模型成功后不再尝试后续模型；链首为主模型，其余为降级备选
DEFAULT_MODEL_CHAIN = ("qwen-plus", "qwen-turbo")


class LLMGateway:
    """按降级链执行流式生成。

    Args:
        provider: 模型供应商适配器。
        chain: 模型降级链，按顺序尝试。
        ledger: 用量记账；缺省使用进程内实现。
        stats: 可选的外部指标对象。

    Raises:
        ValueError: 降级链为空。
    """

    def __init__(
        self,
        provider: ChatProvider,
        chain: Sequence[str] = DEFAULT_MODEL_CHAIN,
        *,
        ledger: TokenLedger | None = None,
        stats: GatewayStats | None = None,
    ) -> None:
        if not chain:
            raise ValueError("model chain must not be empty")
        self._provider = provider
        self._chain = tuple(chain)
        self._ledger = ledger or InMemoryTokenLedger()
        self.stats = stats or GatewayStats()

    @property
    def chain(self) -> tuple[str, ...]:
        """当前降级链。"""
        return self._chain

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        session_id: str,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> Iterator[StreamChunk]:
        """按降级链流式生成。

        首个分片产出前失败会切换下一个模型；已产出分片后失败直接抛出，
        由调用方丢弃该条并计数，避免把半句话拼接成错误回复。

        Args:
            messages: 对话消息。
            session_id: 场次标识，用于用量归集。
            max_tokens: 输出 token 上限。
            temperature: 采样温度。

        Yields:
            流式分片。

        Raises:
            LLMUnavailable: 降级链全部失败。
            LLMError: 不可重试错误，或流式中途失败。
        """
        last_error: LLMError | None = None
        for model in self._chain:
            emitted = False
            usage = Usage()
            try:
                for chunk in self._provider.stream(
                    model,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ):
                    if chunk.usage is not None:
                        usage = usage + chunk.usage
                    emitted = True
                    yield chunk
            except LLMError as error:
                if emitted:
                    self.stats.failed += 1
                    raise
                if not error.retryable:
                    self.stats.failed += 1
                    raise
                last_error = error
                self.stats.fallbacks += 1
                continue
            self.stats.served += 1
            if usage.tokens_in or usage.tokens_out:
                self._ledger.record(
                    session_id=session_id, model=model, usage=usage
                )
            return
        self.stats.failed += 1
        raise LLMUnavailable(len(self._chain)) from last_error
