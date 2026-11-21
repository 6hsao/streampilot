"""编排服务装配：从环境变量构造 LLM/TTS 运行时。

缺少 ``DASHSCOPE_API_KEY`` 时不阻断服务启动：LLM 走话术降级，
TTS 降级为字幕，便于本地无密钥联调；配置了密钥即接入百炼。
"""

import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.sink import LoggingControlSink
from streampilot.llm.errors import UpstreamUnavailable
from streampilot.llm.gateway import LLMGateway
from streampilot.llm.provider import DASHSCOPE_BASE_URL, DashScopeProvider
from streampilot.llm.settings import LLMSettings
from streampilot.llm.types import ChatMessage, StreamChunk
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.runtime import (
    DEFAULT_SYSTEM_PROMPT,
    DialogueRuntime,
    LoggingPlaybackSink,
    PlaybackSink,
    ReplySettings,
    StaticPromptResolver,
)
from streampilot.orchestrator.state import SessionStateMachine
from streampilot.safety.dfa import SafetyEngine
from streampilot.safety.models import RuleSet
from streampilot.safety.rules import load_rules
from streampilot.safety.service import SafetyService
from streampilot.session.models import SessionConfig
from streampilot.tts.cosyvoice import (
    DASHSCOPE_WS_URL,
    DEFAULT_TTS_MODEL,
    CosyVoiceSynthesizer,
)
from streampilot.tts.errors import TTSUnavailable
from streampilot.tts.gateway import TTSGateway
from streampilot.tts.models import (
    AudioChunk,
    AudioFormat,
    SynthesisRequest,
)

# 默认敏感词表位置；缺失时按空规则启动
DEFAULT_LEXICON_PATH = Path("data/safety/lexicon.yml")
# 未配置密钥时使用的占位模型名
UNAVAILABLE_MODEL = "unavailable"
# 缺少密钥时的统一提示
MISSING_KEY = "DASHSCOPE_API_KEY is not configured"

# 场次运行时工厂，签名与 SessionManager 保持一致
RuntimeFactory = Callable[[str, SessionConfig], SessionControl]


@dataclass(frozen=True)
class RuntimeSettings:
    """编排运行时配置。

    Attributes:
        llm: LLM 配置；未配置密钥时为 None。
        tts_url: CosyVoice WebSocket 地址。
        tts_model: CosyVoice 模型名。
        audio_format: 合成音频格式。
        tts_max_concurrency: 进程内并发合成上限。
        system_prompt: 默认系统提示词。
        lexicon_path: 敏感词规则文件。
        history_turns: 上下文保留轮数。
    """

    llm: LLMSettings | None = None
    tts_url: str = DASHSCOPE_WS_URL
    tts_model: str = DEFAULT_TTS_MODEL
    audio_format: AudioFormat = AudioFormat.PCM_16K_MONO
    tts_max_concurrency: int = 2
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    lexicon_path: Path = DEFAULT_LEXICON_PATH
    history_turns: int = 6

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "RuntimeSettings":
        """从环境变量读取运行时配置。

        Args:
            env: 环境变量映射；缺省读取当前进程环境。

        Returns:
            运行时配置。

        Raises:
            RuntimeError: API Key 过短。
            ValueError: 音频格式、并发数或上下文轮数非法。
        """
        data = os.environ if env is None else env
        api_key = data.get("DASHSCOPE_API_KEY", "").strip()
        llm = LLMSettings.from_env(data) if api_key else None
        raw_format = data.get("TTS_AUDIO_FORMAT", "").strip()
        try:
            audio_format = (
                AudioFormat(raw_format)
                if raw_format
                else AudioFormat.PCM_16K_MONO
            )
        except ValueError as exc:
            raise ValueError(
                f"unknown TTS_AUDIO_FORMAT: {raw_format!r}"
            ) from exc
        concurrency = int(data.get("TTS_MAX_CONCURRENCY", "2").strip() or "2")
        if concurrency < 1:
            raise ValueError("TTS_MAX_CONCURRENCY must be at least 1")
        history = int(
            data.get("ORCHESTRATOR_HISTORY_TURNS", "6").strip() or "6"
        )
        if history < 0:
            raise ValueError("ORCHESTRATOR_HISTORY_TURNS must not be negative")
        prompt = (
            data.get("PERSONA_SYSTEM_PROMPT", "").strip()
            or DEFAULT_SYSTEM_PROMPT
        )
        return cls(
            llm=llm,
            tts_url=data.get("COSYVOICE_WS_URL", "").strip()
            or DASHSCOPE_WS_URL,
            tts_model=data.get("COSYVOICE_MODEL", "").strip()
            or DEFAULT_TTS_MODEL,
            audio_format=audio_format,
            tts_max_concurrency=concurrency,
            system_prompt=prompt,
            lexicon_path=Path(
                data.get("SENSITIVE_WORD_DICT_PATH", "").strip()
                or DEFAULT_LEXICON_PATH
            ),
            history_turns=history,
        )

    @property
    def base_url(self) -> str:
        """LLM 基础地址；未配置时返回官方地址。"""
        if self.llm is None:
            return DASHSCOPE_BASE_URL
        return self.llm.base_url

    @property
    def api_key(self) -> str:
        """API Key；未配置时为空串。"""
        return self.llm.api_key if self.llm else ""


class UnavailableChatProvider:
    """未配置密钥时的占位适配器，直接触发话术降级。"""

    name = "unavailable"

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """抛出可重试错误，交由网关耗尽降级链。"""
        raise UpstreamUnavailable(MISSING_KEY)


class UnavailableSynthesizer:
    """未配置密钥时的占位合成器，触发字幕降级。"""

    name = "unavailable"

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        """直接抛出不可用错误。"""
        raise TTSUnavailable(MISSING_KEY)


class RuntimeRegistry:
    """场次运行时注册表，供评论入口按场次查找。"""

    def __init__(self) -> None:
        self._runtimes: dict[str, DialogueRuntime] = {}

    def add(self, runtime: DialogueRuntime) -> None:
        """登记运行时，同一场次覆盖旧值。"""
        self._runtimes[runtime.stream_id] = runtime

    def get(self, stream_id: str) -> DialogueRuntime | None:
        """按场次取运行时。"""
        return self._runtimes.get(stream_id)

    def close_all(self) -> None:
        """停止全部后台线程，用于应用退出。"""
        for runtime in list(self._runtimes.values()):
            runtime.close()
        self._runtimes.clear()


def build_runtime_factory(
    *,
    control: ControlService,
    settings: RuntimeSettings,
    templates: Mapping[str, str] | None = None,
    playback: PlaybackSink | None = None,
    logger: logging.Logger | None = None,
) -> tuple[RuntimeFactory, RuntimeRegistry]:
    """构造运行时工厂与注册表。

    Args:
        control: 控制面服务，运行时通过它取话。
        settings: 运行时配置。
        templates: 预设话术，注入新建场次。
        playback: 播放侧实现；缺省使用日志实现。
        logger: 日志记录器。

    Returns:
        符合 ``SessionManager`` 约定的工厂，以及运行时注册表。
    """
    log = logger or logging.getLogger("streampilot.orchestrator")
    gateway = _build_gateway(settings)
    tts = _build_tts(settings)
    safety = _build_safety(settings.lexicon_path, log)
    prompts = StaticPromptResolver(settings.system_prompt)
    sink = playback or LoggingPlaybackSink(log)
    reply = ReplySettings(
        max_tokens=settings.llm.max_tokens if settings.llm else 200,
        temperature=settings.llm.temperature if settings.llm else 0.7,
        history_turns=settings.history_turns,
    )
    registry = RuntimeRegistry()

    def factory(stream_id: str, config: SessionConfig) -> SessionControl:
        """创建场次控制引用并接上对话运行时。"""
        session = SessionControl(
            stream_id=stream_id,
            state=SessionStateMachine(),
            queue=InMemorySpeechQueue(),
            sink=LoggingControlSink(log),
            templates=dict(templates or {}),
            persona_version_id=(
                f"{config.persona.persona_id}:v{config.persona.version}"
            ),
        )
        runtime = DialogueRuntime(
            control=session,
            control_service=control,
            config=config,
            gateway=gateway,
            tts=tts,
            safety=safety,
            prompts=prompts,
            playback=sink,
            templates=session.templates,
            settings=reply,
            logger=log,
        )
        registry.add(runtime)
        return session

    return factory, registry


def _build_gateway(settings: RuntimeSettings) -> LLMGateway:
    """按配置构造 LLM 网关。"""
    if settings.llm is None:
        return LLMGateway(
            UnavailableChatProvider(), chain=(UNAVAILABLE_MODEL,)
        )
    llm = settings.llm
    provider = DashScopeProvider(llm.api_key, base_url=llm.base_url)
    return LLMGateway(provider, chain=llm.model_chain)


def _build_tts(settings: RuntimeSettings) -> TTSGateway:
    """按配置构造合成网关。"""
    if settings.llm is None:
        synthesizer: CosyVoiceSynthesizer | UnavailableSynthesizer = (
            UnavailableSynthesizer()
        )
    else:
        synthesizer = CosyVoiceSynthesizer(
            settings.llm.api_key,
            url=settings.tts_url,
            model=settings.tts_model,
        )
    return TTSGateway(
        synthesizer, max_concurrency=settings.tts_max_concurrency
    )


def _build_safety(lexicon_path: Path, logger: logging.Logger) -> SafetyService:
    """加载本地规则并构造审核服务。"""
    if lexicon_path.is_file():
        rules = load_rules(lexicon_path)
    else:
        logger.warning(
            "lexicon file not found, using empty rules: %s", lexicon_path
        )
        rules = RuleSet()
    return SafetyService(SafetyEngine(rules))
