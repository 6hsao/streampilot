"""LLM 薄网关：适配器、降级链、流式与记账。"""

from streampilot.llm.errors import (
    AuthFailed,
    BadRequest,
    LLMError,
    LLMUnavailable,
    RateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from streampilot.llm.gateway import DEFAULT_MODEL_CHAIN, LLMGateway
from streampilot.llm.ledger import InMemoryTokenLedger, TokenLedger
from streampilot.llm.provider import ChatProvider, DashScopeProvider
from streampilot.llm.settings import LLMSettings
from streampilot.llm.types import (
    ChatMessage,
    GatewayStats,
    LedgerEntry,
    StreamChunk,
    Usage,
)

__all__ = [
    "DEFAULT_MODEL_CHAIN",
    "AuthFailed",
    "BadRequest",
    "ChatMessage",
    "ChatProvider",
    "DashScopeProvider",
    "GatewayStats",
    "InMemoryTokenLedger",
    "LLMError",
    "LLMGateway",
    "LLMSettings",
    "LLMUnavailable",
    "LedgerEntry",
    "RateLimited",
    "StreamChunk",
    "TokenLedger",
    "UpstreamTimeout",
    "UpstreamUnavailable",
    "Usage",
]
