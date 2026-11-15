"""场次编排状态机。

自研轻量状态机，不引入 LangGraph；单写者由调用方（会话回合循环）保证。
状态与允许迁移见 docs/ARCHITECTURE.md。
"""

from dataclasses import dataclass, field
from enum import StrEnum


class SessionState(StrEnum):
    """场次状态。"""

    IDLE = "idle"
    INTERACTING = "interacting"
    SPEAKING = "speaking"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"
    TAKEOVER = "takeover"
    ENDED = "ended"


ALLOWED: dict[SessionState, frozenset[SessionState]] = {
    SessionState.IDLE: frozenset(
        {
            SessionState.INTERACTING,
            SessionState.TAKEOVER,
            SessionState.CIRCUIT_OPEN,
            SessionState.ENDED,
        }
    ),
    SessionState.INTERACTING: frozenset(
        {
            SessionState.SPEAKING,
            SessionState.COOLDOWN,
            SessionState.CIRCUIT_OPEN,
            SessionState.TAKEOVER,
            SessionState.ENDED,
        }
    ),
    SessionState.SPEAKING: frozenset(
        {
            SessionState.COOLDOWN,
            SessionState.INTERACTING,
            SessionState.CIRCUIT_OPEN,
            SessionState.TAKEOVER,
            SessionState.ENDED,
        }
    ),
    SessionState.COOLDOWN: frozenset(
        {
            SessionState.IDLE,
            SessionState.INTERACTING,
            SessionState.CIRCUIT_OPEN,
            SessionState.TAKEOVER,
            SessionState.ENDED,
        }
    ),
    SessionState.CIRCUIT_OPEN: frozenset(
        {
            SessionState.IDLE,
            SessionState.TAKEOVER,
            SessionState.ENDED,
        }
    ),
    SessionState.TAKEOVER: frozenset({SessionState.IDLE, SessionState.ENDED}),
    SessionState.ENDED: frozenset(),
}


class InvalidTransition(ValueError):
    """非法状态迁移。

    Args:
        current: 当前状态。
        target: 目标状态。
    """

    def __init__(self, current: SessionState, target: SessionState) -> None:
        super().__init__(f"illegal transition: {current} -> {target}")
        self.current = current
        self.target = target


@dataclass
class SessionStateMachine:
    """单场次状态机。

    Attributes:
        state: 当前状态。
        history: 迁移历史，元素为 (目标状态, 原因)。
    """

    state: SessionState = SessionState.IDLE
    history: list[tuple[SessionState, str]] = field(default_factory=list)

    def transition(
        self, target: SessionState, reason: str = ""
    ) -> SessionState:
        """执行一次状态迁移。

        Args:
            target: 目标状态。
            reason: 迁移原因，用于审计。

        Returns:
            迁移后的状态。

        Raises:
            InvalidTransition: 迁移不在允许集合内。
        """
        if target not in ALLOWED[self.state]:
            raise InvalidTransition(self.state, target)
        self.state = target
        self.history.append((target, reason))
        return self.state

    def takeover(self, operator: str) -> SessionState:
        """人工接管，优先级最高。

        Args:
            operator: 操作人标识。

        Returns:
            迁移后的状态。

        Raises:
            InvalidTransition: 场次已结束。
        """
        return self.transition(SessionState.TAKEOVER, f"takeover:{operator}")

    def trip(self, reason: str) -> SessionState:
        """触发安全熔断。

        Args:
            reason: 熔断原因。

        Returns:
            迁移后的状态。
        """
        return self.transition(SessionState.CIRCUIT_OPEN, f"trip:{reason}")

    def reset(self, operator: str) -> SessionState:
        """人工确认后解除熔断，回到待机。

        Args:
            operator: 确认人标识。

        Returns:
            迁移后的状态。
        """
        if self.state is not SessionState.CIRCUIT_OPEN:
            raise InvalidTransition(self.state, SessionState.IDLE)
        return self.transition(SessionState.IDLE, f"reset:{operator}")

    def end(self) -> SessionState:
        """收播，进入终态。"""
        return self.transition(SessionState.ENDED, "end")
