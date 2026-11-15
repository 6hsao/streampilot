"""token 记账：按场次与模型归集用量。"""

from typing import Protocol

from streampilot.llm.types import LedgerEntry, Usage


class TokenLedger(Protocol):
    """用量记账协议。"""

    def record(self, *, session_id: str, model: str, usage: Usage) -> None:
        """记录一次完成的请求。

        Args:
            session_id: 场次标识。
            model: 模型名称。
            usage: token 用量。
        """
        ...


class InMemoryTokenLedger:
    """进程内记账，用于单测与单实例部署。

    生产环境由 OPS-01 落库归集，这里保持最小实现。
    """

    def __init__(self) -> None:
        self.by_session: dict[str, LedgerEntry] = {}
        self.by_model: dict[str, LedgerEntry] = {}

    def record(self, *, session_id: str, model: str, usage: Usage) -> None:
        """同时计入场次与模型两个维度。"""
        self.by_session.setdefault(session_id, LedgerEntry()).add(usage)
        self.by_model.setdefault(model, LedgerEntry()).add(usage)
