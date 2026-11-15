"""按人设版本组装提示词消息。"""

from collections.abc import Sequence

from streampilot.llm.types import ChatMessage
from streampilot.persona.models import PersonaVersion

# 上下文默认保留轮数，与 ARCHITECTURE 的「最近 6 轮」一致
DEFAULT_HISTORY_TURNS = 6


def compose_messages(
    version: PersonaVersion,
    comment: str,
    *,
    history: Sequence[ChatMessage] = (),
    max_history: int = DEFAULT_HISTORY_TURNS,
) -> tuple[ChatMessage, ...]:
    """组装一次回复所需的完整消息列表。

    Args:
        version: 生效的人设版本。
        comment: 当前观众评论。
        history: 历史对话，按时间正序。
        max_history: 最多保留的历史条数。

    Returns:
        系统提示词 + 截断后的历史 + 当前评论。

    Raises:
        ValueError: 评论为空或历史条数上限为负。
    """
    if not comment.strip():
        raise ValueError("comment must not be empty")
    if max_history < 0:
        raise ValueError("max_history must not be negative")
    messages = [
        ChatMessage(role="system", content=version.render_system_prompt())
    ]
    messages.extend(list(history)[-max_history:] if max_history else [])
    messages.append(ChatMessage(role="user", content=comment.strip()))
    return tuple(messages)
