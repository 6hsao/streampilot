"""单个场次的可控运行时引用。"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from streampilot.control.sink import ControlSink
from streampilot.orchestrator.queue import SpeechQueue
from streampilot.orchestrator.state import (
    SessionState,
    SessionStateMachine,
)


@dataclass
class SessionControl:
    """单个场次的可控运行时引用。

    Attributes:
        stream_id: 场次标识。
        state: 场次状态机。
        queue: 待发声队列。
        sink: 下游动作接口。
        templates: 预设话术，键为 template_id。
        paused: 是否暂停 AI 消费。
        muted: 是否静音。
        persona_version_id: 当前人设版本。
        resume_state: 接管前的状态，释放时用于恢复熔断语义。
    """

    stream_id: str
    state: SessionStateMachine
    queue: SpeechQueue
    sink: ControlSink
    templates: Mapping[str, str] = field(default_factory=dict)
    paused: bool = False
    muted: bool = False
    persona_version_id: str | None = None
    resume_state: SessionState | None = None

    def __post_init__(self) -> None:
        if not self.stream_id.strip():
            raise ValueError("stream_id must not be empty")

    @property
    def ai_silent(self) -> bool:
        """AI 是否必须完全静默。

        接管、暂停、静音或熔断任一成立即静默；熔断由安全侧触发，
        控制面只读状态，不负责复位。
        """
        return (
            self.state.state
            in (
                SessionState.TAKEOVER,
                SessionState.CIRCUIT_OPEN,
            )
            or self.paused
            or self.muted
        )
