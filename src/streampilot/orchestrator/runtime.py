"""编排运行时：评论 → LLM → 输出审核 → TTS → 播放。

每场次一个运行时，后台线程按需消费发言队列；取话统一经过
:meth:`ControlService.dequeue`，接管、暂停、静音或熔断时不发声。
播放通过 :class:`PlaybackSink` 与渲染/推流解耦，本地联调用日志实现。
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from streampilot.contracts.ids import new_id
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.llm.errors import LLMError
from streampilot.llm.gateway import LLMGateway
from streampilot.llm.types import ChatMessage
from streampilot.orchestrator.queue import (
    DEFAULT_TTL_MS,
    EnqueueResult,
    Priority,
    QueuedTurn,
)
from streampilot.orchestrator.state import (
    InvalidTransition,
    SessionState,
)
from streampilot.persona.models import PersonaVersion
from streampilot.persona.prompt import compose_messages
from streampilot.safety.output import (
    DEFAULT_FALLBACK_PHRASE,
    OutputGuard,
)
from streampilot.safety.service import SafetyService
from streampilot.session.models import SessionConfig
from streampilot.tts.errors import TTSError
from streampilot.tts.gateway import TTSGateway
from streampilot.tts.models import (
    AudioChunk,
    AudioFormat,
    SynthesisRequest,
)
from streampilot.tts.normalize import normalize_for_tts

# 未配置人设时的默认系统提示词
DEFAULT_SYSTEM_PROMPT = (
    "你是直播间的 AI 主播，用简体中文口语化地回答观众问题。"
    "回复要短，一次最多三句话，不承诺商品功效与价格。"
)
# LLM 失败时优先使用的话术标识
FALLBACK_TEMPLATE_ID = "break"
# 后台线程空闲时的等待时长，秒
WORKER_IDLE_SECONDS = 0.2
# 单条评论默认优先级
DEFAULT_PRIORITY = Priority.NORMAL
# 视为“未配置”的占位音色，命中时直接报错而不是悄悄合成
PLACEHOLDER_VOICE_IDS: frozenset[str] = frozenset(
    {"", "replace_with_authorized_voice_id"}
)
# 占位音色常见的形状特征，用于拦截未替换的模板值
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "replace_with",
    "your_voice",
    "voice_authorized_00",
)


def _is_placeholder_voice(voice: str) -> bool:
    """判断音色是否为空或未替换的占位值。

    Args:
        voice: 已去除首尾空白的音色标识。

    Returns:
        需要视为“未配置”时返回 True。
    """
    lowered = voice.lower()
    return lowered in PLACEHOLDER_VOICE_IDS or any(
        marker in lowered for marker in PLACEHOLDER_MARKERS
    )


def _require_voice(voice_id: str | None, default: str = "") -> str:
    """解析生效音色：优先场次配置，占位或缺失时回退到默认音色。

    Args:
        voice_id: 场次配置里的音色标识。
        default: 环境变量配置的默认音色。

    Returns:
        去除首尾空白后的可用音色标识。

    Raises:
        ValueError: 场次音色与默认音色都不可用，提示去配置真实音色。
    """
    for candidate in ((voice_id or "").strip(), default.strip()):
        if candidate and not _is_placeholder_voice(candidate):
            return candidate
    raise ValueError(
        "音色未配置：请把场次 voice_id 或环境变量 "
        "COSYVOICE_DEFAULT_VOICE 设为已授权的 CosyVoice 音色"
        "（例如 longfeifei_v2），"
        f"当前值 {voice_id or '<空>'!r} / 默认 {default or '<空>'!r} "
        "均不可用"
    )


class CommentOutcome(StrEnum):
    """评论提交结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED_FULL = "rejected_full"
    BLOCKED = "blocked"
    SILENT = "silent"


@dataclass
class RuntimeStats:
    """运行时计数，用于本地观测与测试断言。

    Attributes:
        comments_accepted: 受理并入队的评论数。
        comments_blocked: 输入审核拦截的评论数。
        comments_silent: 静默期间拒收的评论数。
        turns_spoken: 完成发声的回合数。
        sentences_spoken: 已发声句子数。
        llm_failures: LLM 最终失败次数。
        tts_fallbacks: 降级为字幕的句子数。
    """

    comments_accepted: int = 0
    comments_blocked: int = 0
    comments_silent: int = 0
    turns_spoken: int = 0
    sentences_spoken: int = 0
    llm_failures: int = 0
    tts_fallbacks: int = 0


@dataclass(frozen=True)
class ReplySettings:
    """单场次回复参数。

    Attributes:
        max_tokens: 单次 LLM 输出上限。
        temperature: 采样温度。
        history_turns: 上下文保留轮数，一轮为评论加回复。
        audio_format: 合成音频格式。
        speech_rate: 语速倍率。
        fallback_phrase: 兜底话术。
        ttl_ms: 评论在队列中的最长存活时间。
    """

    max_tokens: int = 200
    temperature: float = 0.7
    history_turns: int = 6
    audio_format: AudioFormat = AudioFormat.PCM_16K_MONO
    speech_rate: float = 1.0
    fallback_phrase: str = DEFAULT_FALLBACK_PHRASE
    ttl_ms: int = DEFAULT_TTL_MS

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.history_turns < 0:
            raise ValueError("history_turns must not be negative")
        if not self.fallback_phrase.strip():
            raise ValueError("fallback_phrase must not be empty")
        if self.ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")


class PlaybackSink(Protocol):
    """播放侧协议：合成音频或降级字幕。"""

    def play(self, text: str, audio: Sequence[AudioChunk]) -> None:
        """播放一句合成音频。

        Args:
            text: 该句文本，用于字幕与唇形对齐。
            audio: 有序音频分片。
        """
        ...

    def subtitle(self, text: str) -> None:
        """TTS 不可用时只上字幕。

        Args:
            text: 待展示文本。
        """
        ...


class LoggingPlaybackSink:
    """只记录播放动作的占位实现，供本地联调使用。

    Args:
        logger: 日志记录器，默认使用 streampilot.orchestrator。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("streampilot.orchestrator")

    def play(self, text: str, audio: Sequence[AudioChunk]) -> None:
        """记录句子、分片数与总时长。"""
        total_ms = sum(item.duration_ms for item in audio)
        self._logger.info(
            "play %d char(s), %d chunk(s), %d ms: %s",
            len(text),
            len(audio),
            total_ms,
            text,
        )

    def subtitle(self, text: str) -> None:
        """记录字幕降级。"""
        self._logger.info("subtitle only: %s", text)


class PromptResolver(Protocol):
    """按场次配置解析人设版本。"""

    def resolve(self, config: SessionConfig) -> PersonaVersion:
        """返回本次生效的人设版本。

        Args:
            config: 场次配置快照。
        """
        ...


@dataclass(frozen=True)
class StaticPromptResolver:
    """使用固定提示词的人设解析器。

    人设服务（DLG-04）接入后替换为按版本查询的实现。

    Args:
        system_prompt: 系统提示词主体。
        style_rules: 风格规则。
        banned_topics: 禁止讨论的话题。

    Raises:
        ValueError: 系统提示词为空。
    """

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    style_rules: tuple[str, ...] = ()
    banned_topics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.system_prompt.strip():
            raise ValueError("system_prompt must not be empty")

    def resolve(self, config: SessionConfig) -> PersonaVersion:
        """构造与场次版本号一致的人设版本。"""
        return PersonaVersion(
            version=max(config.persona.version, 1),
            system_prompt=self.system_prompt,
            style_rules=self.style_rules,
            banned_topics=self.banned_topics,
        )


class DialogueRuntime:
    """单场次对话运行时。

    Args:
        control: 场次控制引用，提供状态机、队列与预设话术。
        control_service: 取话入口，保证静默语义。
        config: 场次配置快照。
        gateway: LLM 网关。
        tts: 合成网关。
        safety: 输入与输出审核服务。
        prompts: 人设解析器。
        playback: 播放侧实现。
        templates: 预设话术，键为 template_id。
        settings: 回复参数。
        clock_ms: 毫秒时间源，便于测试注入。
        logger: 日志记录器。
        worker_enabled: 是否允许启动后台消费线程。
        default_voice: 场次音色缺失或为占位符时使用的默认音色。

    Raises:
        ValueError: 场次音色与默认音色都不可用。
    """

    def __init__(
        self,
        *,
        control: SessionControl,
        control_service: ControlService,
        config: SessionConfig,
        gateway: LLMGateway,
        tts: TTSGateway,
        safety: SafetyService,
        prompts: PromptResolver,
        playback: PlaybackSink,
        templates: Mapping[str, str] | None = None,
        settings: ReplySettings | None = None,
        clock_ms: Callable[[], int] | None = None,
        logger: logging.Logger | None = None,
        worker_enabled: bool = True,
        default_voice: str = "",
    ) -> None:
        self._control = control
        self._service = control_service
        self._config = config
        self._gateway = gateway
        self._tts = tts
        self._safety = safety
        self._prompts = prompts
        self._playback = playback
        self._templates = dict(templates or {})
        self._settings = settings or ReplySettings()
        self._guard = OutputGuard(
            safety, fallback_phrase=self._settings.fallback_phrase
        )
        self._clock_ms = clock_ms or _default_clock_ms
        self._logger = logger or logging.getLogger("streampilot.orchestrator")
        self._history: list[ChatMessage] = []
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_enabled = worker_enabled
        self._voice_id = _require_voice(config.voice_id, default_voice)
        self.stats = RuntimeStats()

    @property
    def stream_id(self) -> str:
        """场次标识。"""
        return self._control.stream_id

    @property
    def running(self) -> bool:
        """后台线程是否在运行。"""
        return self._thread is not None and self._thread.is_alive()

    def submit_comment(
        self,
        text: str,
        *,
        item_id: str | None = None,
        priority: Priority = DEFAULT_PRIORITY,
        now_ms: int | None = None,
    ) -> CommentOutcome:
        """审核并提交一条观众评论。

        Args:
            text: 评论正文。
            item_id: 评论唯一标识，缺省生成。
            priority: 发言优先级。
            now_ms: 入队时间，缺省取当前时间。

        Returns:
            提交结果。

        Raises:
            ValueError: 评论为空。
        """
        comment = text.strip()
        if not comment:
            raise ValueError("comment must not be empty")
        if self._silenced():
            self.stats.comments_silent += 1
            return CommentOutcome.SILENT
        if not self._safety.check(comment, "input").allowed:
            self.stats.comments_blocked += 1
            return CommentOutcome.BLOCKED
        now = self._clock_ms() if now_ms is None else now_ms
        turn = QueuedTurn(
            item_id=item_id or new_id(),
            text=comment,
            enqueued_at_ms=now,
            expires_at_ms=now + self._settings.ttl_ms,
            priority=priority,
        )
        result = self._control.queue.enqueue(turn)
        outcome = _OUTCOMES[result]
        if outcome is CommentOutcome.ACCEPTED:
            self.stats.comments_accepted += 1
            self.start()
            self._wakeup.set()
        return outcome

    def start(self) -> None:
        """启动后台消费线程，重复调用无副作用。"""
        if not self._worker_enabled or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"orchestrator-{self.stream_id}",
            daemon=True,
        )
        self._thread.start()

    def close(self, *, timeout_s: float = 2.0) -> None:
        """停止后台线程。

        Args:
            timeout_s: 等待线程退出的最长时间，秒。
        """
        self._stop.set()
        self._wakeup.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout_s)
        if thread.is_alive():
            self._logger.warning(
                "runtime thread for %s did not stop", self.stream_id
            )
        self._thread = None

    def run_once(self, now_ms: int | None = None) -> bool:
        """尝试处理一条待发言。

        Args:
            now_ms: 当前时间，缺省取毫秒时间戳。

        Returns:
            处理了一条发言返回 True；空队列或静默时返回 False。
        """
        if self._silenced():
            return False
        now = self._clock_ms() if now_ms is None else now_ms
        turn = self._service.dequeue(self.stream_id, now)
        if turn is None:
            return False
        self._respond(turn)
        return True

    def _loop(self) -> None:
        """后台循环：有活就继续，空闲则短等唤醒。"""
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception as exc:  # noqa: BLE001 - 单条失败不能终止循环
                self._logger.exception(
                    "runtime loop error for %s: %s", self.stream_id, exc
                )
                busy = False
            if busy:
                continue
            self._wakeup.wait(WORKER_IDLE_SECONDS)
            self._wakeup.clear()

    def _respond(self, turn: QueuedTurn) -> None:
        """完成一轮回复：生成、审核、合成与播放。"""
        self._transition(SessionState.INTERACTING, "comment")
        messages = compose_messages(
            self._prompts.resolve(self._config),
            turn.text,
            history=self._history,
            max_history=self._settings.history_turns * 2,
        )
        spoken: list[str] = []
        chunks = self._gateway.stream(
            messages,
            session_id=self.stream_id,
            max_tokens=self._settings.max_tokens,
            temperature=self._settings.temperature,
        )
        try:
            for sentence in self._guard.guard(chunks):
                if self._silenced():
                    break
                text = normalize_for_tts(sentence.text)
                if not text.strip():
                    continue
                self._speak(text)
                spoken.append(sentence.text)
                self.stats.sentences_spoken += 1
        except LLMError as exc:
            self.stats.llm_failures += 1
            self._logger.warning("llm failed for %s: %s", self.stream_id, exc)
        if spoken:
            self.stats.turns_spoken += 1
            self._remember(turn.text, "".join(spoken))
        elif not self._silenced():
            self._speak_fallback()
        self._transition(SessionState.COOLDOWN, "turn done")
        self._transition(SessionState.IDLE, "cooldown end")

    def _speak(self, text: str) -> None:
        """合成一句并交给播放侧；失败降级为字幕。"""
        self._transition(SessionState.SPEAKING, "speak")
        request = SynthesisRequest(
            text=text,
            voice_id=self._voice_id,
            audio_format=self._settings.audio_format,
            speech_rate=self._settings.speech_rate,
        )
        try:
            audio = list(
                self._tts.synthesize(request, session_id=self.stream_id)
            )
        except TTSError as exc:
            self.stats.tts_fallbacks += 1
            self._logger.warning("tts failed, subtitle only: %s", exc)
            self._playback.subtitle(text)
            return
        if not audio:
            self.stats.tts_fallbacks += 1
            self._playback.subtitle(text)
            return
        self._playback.play(text, audio)

    def _speak_fallback(self) -> None:
        """LLM 无可用输出时播放预设话术或兜底句。"""
        phrase = self._templates.get(FALLBACK_TEMPLATE_ID)
        text = phrase or self._settings.fallback_phrase
        if not self._safety.check(text, "output").allowed:
            return
        normalized = normalize_for_tts(text)
        if not normalized.strip():
            return
        self._speak(normalized)
        self.stats.sentences_spoken += 1

    def _silenced(self) -> bool:
        """接管、暂停、静音、熔断或收播后都必须静默。"""
        return (
            self._control.ai_silent
            or self._control.state.state is SessionState.ENDED
        )

    def _remember(self, comment: str, reply: str) -> None:
        """维护最近若干轮对话上下文。"""
        self._history.append(ChatMessage(role="user", content=comment))
        self._history.append(ChatMessage(role="assistant", content=reply))
        limit = self._settings.history_turns * 2
        if limit <= 0:
            self._history.clear()
        elif len(self._history) > limit:
            del self._history[:-limit]

    def _transition(self, target: SessionState, reason: str) -> None:
        """状态迁移；接管或收播导致的非法迁移直接忽略。"""
        try:
            self._control.state.transition(target, reason)
        except InvalidTransition:
            self._logger.debug(
                "skip transition %s -> %s", self._control.state.state, target
            )


_OUTCOMES: dict[EnqueueResult, CommentOutcome] = {
    EnqueueResult.ACCEPTED: CommentOutcome.ACCEPTED,
    EnqueueResult.DUPLICATE: CommentOutcome.DUPLICATE,
    EnqueueResult.REJECTED_FULL: CommentOutcome.REJECTED_FULL,
}


def _default_clock_ms() -> int:
    """返回当前毫秒时间戳。"""
    return int(time.time() * 1000)
