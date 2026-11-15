"""LLM 网关的请求、响应与记账数据类型。"""

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    """一条对话消息。

    Attributes:
        role: 消息角色。
        content: 消息文本。
    """

    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    """token 用量。

    Attributes:
        tokens_in: 输入 token 数。
        tokens_out: 输出 token 数。
    """

    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
        )


@dataclass(frozen=True)
class StreamChunk:
    """流式输出的一个分片。

    Attributes:
        delta: 新增文本。
        finish_reason: 结束原因，通常只在最后一片出现。
        usage: token 用量，通常只在最后一片出现。
    """

    delta: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass
class LedgerEntry:
    """按维度归集的用量与请求数。

    Attributes:
        requests: 请求次数。
        usage: 累计 token 用量。
    """

    requests: int = 0
    usage: Usage = field(default_factory=Usage)

    def add(self, usage: Usage) -> None:
        """累加一次请求。"""
        self.requests += 1
        self.usage = self.usage + usage


@dataclass
class GatewayStats:
    """网关运行指标。

    Attributes:
        served: 成功完成的流数。
        fallbacks: 因可重试错误切换模型的次数。
        failed: 最终失败的流数。
    """

    served: int = 0
    fallbacks: int = 0
    failed: int = 0
