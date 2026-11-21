"""编排运行时、装配与评论入口的测试。"""

import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from streampilot.control.app import create_app
from streampilot.control.models import (
    ControlAction,
    ControlRequest,
    TakeoverAction,
)
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.sink import LoggingControlSink
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.llm.errors import (
    LLMError,
    RateLimited,
    UpstreamUnavailable,
)
from streampilot.llm.gateway import LLMGateway
from streampilot.llm.types import ChatMessage, StreamChunk
from streampilot.orchestrator.api import create_runtime_router
from streampilot.orchestrator.bootstrap import (
    RuntimeRegistry,
    RuntimeSettings,
    UnavailableChatProvider,
    UnavailableSynthesizer,
    build_runtime_factory,
)
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.runtime import (
    CommentOutcome,
    DialogueRuntime,
    ReplySettings,
    StaticPromptResolver,
)
from streampilot.orchestrator.state import (
    SessionState,
    SessionStateMachine,
)
from streampilot.persona.models import PersonaSnapshot
from streampilot.safety.dfa import SafetyEngine
from streampilot.safety.models import RuleSet, Severity, WordRule
from streampilot.safety.service import SafetyService
from streampilot.session.app import create_session_router
from streampilot.session.facts import load_fact_source
from streampilot.session.manager import SessionManager
from streampilot.session.models import SessionConfig
from streampilot.tts.errors import TTSUnavailable
from streampilot.tts.gateway import TTSGateway
from streampilot.tts.models import AudioChunk, SynthesisRequest

# 测试场景统一使用的场次标识
STREAM_ID = "st_test"
# 模拟的当前时间
NOW_MS = 1_000


class FakeProvider:
    """按脚本产出分片的供应商。"""

    name = "fake"

    def __init__(self, chunks: Sequence[StreamChunk]) -> None:
        self.chunks = tuple(chunks)
        self.calls = 0
        self.seen: tuple[ChatMessage, ...] = ()

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """记录入参后产出预设分片。"""
        self.calls += 1
        self.seen = tuple(messages)
        yield from self.chunks


class FailingProvider:
    """始终失败的供应商。"""

    name = "failing"

    def __init__(self, error: LLMError) -> None:
        self.error = error

    def stream(
        self,
        model: str,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """直接抛出预设错误。"""
        raise self.error


class FakeSynth:
    """返回固定音频的合成器。"""

    name = "fake"

    def __init__(self, chunks: Sequence[AudioChunk]) -> None:
        self.chunks = tuple(chunks)
        self.requests: list[SynthesisRequest] = []

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        """记录请求并产出预设音频。"""
        self.requests.append(request)
        yield from self.chunks


class FailingSynth:
    """始终失败的合成器。"""

    name = "failing"

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        """直接抛出不可用错误。"""
        raise TTSUnavailable("upstream unavailable")


class FakePlayback:
    """记录播放与字幕的播放侧实现。"""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.subtitles: list[str] = []
        self.spoken = threading.Event()

    def play(self, text: str, audio: Sequence[AudioChunk]) -> None:
        """记录一次播放。"""
        self.played.append(text)
        self.spoken.set()

    def subtitle(self, text: str) -> None:
        """记录一次字幕降级。"""
        self.subtitles.append(text)
        self.spoken.set()


@dataclass
class Harness:
    """运行时测试脚手架。"""

    runtime: DialogueRuntime
    control: SessionControl
    service: ControlService
    playback: FakePlayback
    synth: FakeSynth | FailingSynth


def _audio(seq: int = 0) -> AudioChunk:
    """构造一个合法的 PCM 分片。"""
    return AudioChunk(
        seq=seq,
        data=b"\x00\x00",
        duration_ms=1,
        start_ms=seq,
        sample_rate=16_000,
    )


def _config(voice_id: str = "voice_1") -> SessionConfig:
    """构造测试场次配置。"""
    return SessionConfig(
        persona=PersonaSnapshot(
            persona_id="p_1", version=1, prompt_digest="d_1"
        ),
        voice_id=voice_id,
        avatar_id="avatar_1",
    )


def make_harness(
    *,
    provider: FakeProvider | FailingProvider | None = None,
    synth: FakeSynth | FailingSynth | None = None,
    rules: RuleSet | None = None,
    templates: Mapping[str, str] | None = None,
    worker_enabled: bool = False,
    config: SessionConfig | None = None,
    default_voice: str = "",
) -> Harness:
    """装配一套可控的运行时。"""
    audit = InMemoryAuditStore()
    service = ControlService(audit=audit, takeovers=InMemoryTakeoverStore())
    control = SessionControl(
        stream_id=STREAM_ID,
        state=SessionStateMachine(),
        queue=InMemorySpeechQueue(cooldown_ms=0),
        sink=LoggingControlSink(),
        templates=dict(templates or {}),
    )
    service.register(control)
    playback = FakePlayback()
    chosen_synth = synth or FakeSynth((_audio(),))
    runtime = DialogueRuntime(
        control=control,
        control_service=service,
        config=config or _config(),
        gateway=LLMGateway(
            provider or FakeProvider((StreamChunk(delta="你好。"),)),
            chain=("fake",),
        ),
        tts=TTSGateway(chosen_synth, max_attempts=1),
        safety=SafetyService(SafetyEngine(rules or RuleSet())),
        prompts=StaticPromptResolver(),
        playback=playback,
        templates=control.templates,
        settings=ReplySettings(history_turns=2),
        clock_ms=lambda: NOW_MS,
        logger=logging.getLogger("test.orchestrator"),
        worker_enabled=worker_enabled,
        default_voice=default_voice,
    )
    return Harness(
        runtime=runtime,
        control=control,
        service=service,
        playback=playback,
        synth=chosen_synth,
    )


def test_comment_triggers_llm_and_playback() -> None:
    """评论入队后经 LLM、审核与 TTS 播放。"""
    harness = make_harness()
    assert (
        harness.runtime.submit_comment("在吗", item_id="c1")
        is CommentOutcome.ACCEPTED
    )
    assert harness.runtime.run_once(NOW_MS) is True
    assert harness.playback.played == ["你好。"]
    assert harness.runtime.stats.turns_spoken == 1
    assert harness.control.queue.stats().size == 0
    assert harness.control.state.state is SessionState.IDLE


def test_duplicate_comment_is_ignored() -> None:
    """同一评论标识重复入队只受理一次。"""
    harness = make_harness()
    harness.runtime.submit_comment("在吗", item_id="c1")
    second = harness.runtime.submit_comment("在吗", item_id="c1")
    assert second is CommentOutcome.DUPLICATE
    assert harness.runtime.stats.comments_accepted == 1


def test_input_blocked_comment_is_rejected() -> None:
    """输入审核命中拦截词时不再入队。"""
    rules = RuleSet(
        words=(
            WordRule(term="违禁", category="test", severity=Severity.BLOCK),
        )
    )
    harness = make_harness(rules=rules)
    outcome = harness.runtime.submit_comment("这是违禁内容")
    assert outcome is CommentOutcome.BLOCKED
    assert harness.control.queue.stats().size == 0
    assert harness.runtime.stats.comments_blocked == 1


def test_takeover_blocks_and_clears_queue() -> None:
    """接管后拒收新评论，并清空待发声。"""
    harness = make_harness()
    harness.runtime.submit_comment("在吗", item_id="c1")
    harness.service.takeover(
        STREAM_ID,
        action=TakeoverAction.TAKE,
        operator_id="op_1",
        reason="人工接管",
    )
    assert harness.control.queue.stats().size == 0
    outcome = harness.runtime.submit_comment("还在吗")
    assert outcome is CommentOutcome.SILENT
    assert harness.runtime.run_once(NOW_MS) is False
    assert harness.playback.played == []


def test_end_stream_silences_runtime() -> None:
    """收播后拒收评论且不再出队。"""
    harness = make_harness()
    harness.runtime.submit_comment("在吗")
    harness.service.end_stream(STREAM_ID, reason="test")
    assert harness.runtime.submit_comment("还在吗") is CommentOutcome.SILENT
    assert harness.runtime.run_once(NOW_MS) is False
    assert harness.playback.played == []


def test_pause_stops_dequeue_until_resume() -> None:
    """暂停期间不出队，恢复后继续发声。"""
    harness = make_harness()
    harness.runtime.submit_comment("在吗")
    harness.service.control(
        STREAM_ID,
        request=ControlRequest(action=ControlAction.PAUSE, reason="检查"),
        operator_id="op_1",
    )
    assert harness.runtime.run_once(NOW_MS) is False
    harness.service.control(
        STREAM_ID,
        request=ControlRequest(action=ControlAction.RESUME, reason="恢复"),
        operator_id="op_1",
    )
    assert harness.runtime.run_once(NOW_MS) is True
    assert harness.playback.played == ["你好。"]


def test_blocked_output_uses_guard_fallback() -> None:
    """LLM 输出命中拦截词时只播放兜底话术。"""
    rules = RuleSet(
        words=(
            WordRule(term="违禁", category="test", severity=Severity.BLOCK),
        )
    )
    provider = FakeProvider((StreamChunk(delta="这是违禁内容。"),))
    harness = make_harness(provider=provider, rules=rules)
    harness.runtime.submit_comment("在吗")
    assert harness.runtime.run_once(NOW_MS) is True
    assert len(harness.playback.played) == 1
    assert "违禁" not in harness.playback.played[0]
    assert "换个方向" in harness.playback.played[0]


def test_llm_failure_plays_template() -> None:
    """LLM 最终失败时播放预设话术。"""
    provider = FailingProvider(RateLimited())
    harness = make_harness(
        provider=provider, templates={"break": "稍等一下。"}
    )
    harness.runtime.submit_comment("在吗")
    assert harness.runtime.run_once(NOW_MS) is True
    assert harness.playback.played == ["稍等一下。"]
    assert harness.runtime.stats.llm_failures == 1


def test_llm_failure_without_template_uses_fallback() -> None:
    """没有预设话术时使用审核兜底句。"""
    provider = FailingProvider(RateLimited())
    harness = make_harness(provider=provider)
    harness.runtime.submit_comment("在吗")
    assert harness.runtime.run_once(NOW_MS) is True
    assert "换个方向" in harness.playback.played[0]


def test_tts_failure_falls_back_to_subtitle() -> None:
    """合成失败时只上字幕，不阻塞后续场次。"""
    harness = make_harness(synth=FailingSynth())
    harness.runtime.submit_comment("在吗")
    assert harness.runtime.run_once(NOW_MS) is True
    assert harness.playback.played == []
    assert harness.playback.subtitles == ["你好。"]
    assert harness.runtime.stats.tts_fallbacks == 1


def test_worker_thread_consumes_comment() -> None:
    """后台线程自动消费评论并播放。"""
    harness = make_harness(worker_enabled=True)
    try:
        outcome = harness.runtime.submit_comment("在吗")
        assert outcome is CommentOutcome.ACCEPTED
        assert harness.playback.spoken.wait(2.0)
        assert harness.playback.played == ["你好。"]
    finally:
        harness.runtime.close()
    assert harness.runtime.running is False


def test_history_is_trimmed() -> None:
    """多轮对话只保留最近若干轮。"""
    provider = FakeProvider((StreamChunk(delta="好的。"),))
    harness = make_harness(provider=provider)
    for index in range(3):
        harness.runtime.submit_comment(f"问题{index}")
        assert harness.runtime.run_once(NOW_MS) is True
    # 系统提示词 + 最近两轮历史 + 当前评论
    assert len(provider.seen) <= 6


def test_runtime_settings_defaults_without_key() -> None:
    """缺少密钥时保持可启动，LLM 与 TTS 走降级。"""
    settings = RuntimeSettings.from_env({})
    assert settings.llm is None
    assert settings.history_turns == 6


def test_runtime_settings_reads_overrides() -> None:
    """环境变量覆盖音频格式、并发与提示词。"""
    settings = RuntimeSettings.from_env(
        {
            "DASHSCOPE_API_KEY": "00000000000000000000",
            "TTS_AUDIO_FORMAT": "pcm_24k_mono",
            "TTS_MAX_CONCURRENCY": "1",
            "ORCHESTRATOR_HISTORY_TURNS": "2",
            "PERSONA_SYSTEM_PROMPT": "自定义人设",
        }
    )
    assert settings.llm is not None
    assert settings.audio_format.value == "pcm_24k_mono"
    assert settings.tts_max_concurrency == 1
    assert settings.history_turns == 2
    assert settings.system_prompt == "自定义人设"


def test_runtime_settings_reads_default_voice() -> None:
    """默认音色从环境变量读取，供场次占位音色回退。"""
    settings = RuntimeSettings.from_env(
        {"COSYVOICE_DEFAULT_VOICE": "longfeifei_v2"}
    )
    assert settings.default_voice == "longfeifei_v2"


def test_runtime_settings_rejects_invalid_format() -> None:
    """未知音频格式在启动时直接报错。"""
    with pytest.raises(ValueError, match="TTS_AUDIO_FORMAT"):
        RuntimeSettings.from_env({"TTS_AUDIO_FORMAT": "mp3"})


def test_unavailable_adapters_raise() -> None:
    """未配置密钥的占位适配器给出可分类错误。"""
    with pytest.raises(UpstreamUnavailable):
        UnavailableChatProvider().stream(
            "m", [], max_tokens=1, temperature=0.0
        )
    with pytest.raises(TTSUnavailable):
        UnavailableSynthesizer().synthesize(
            SynthesisRequest(text="你好", voice_id="voice_1")
        )


def _build_app() -> tuple[TestClient, RuntimeRegistry]:
    """装配带评论入口的测试应用。"""
    audit = InMemoryAuditStore()
    control = ControlService(audit=audit, takeovers=InMemoryTakeoverStore())
    settings = RuntimeSettings.from_env({})
    factory, runtimes = build_runtime_factory(
        control=control, settings=settings, templates={}
    )
    manager = SessionManager(
        control=control,
        audit=audit,
        templates={},
        facts=load_fact_source(
            {
                "BROADCAST_MODE": "local",
                "AIGC_LABEL_ENABLED": "1",
                "SAFETY_HEALTHY": "1",
                "LOCAL_BROADCAST_READY": "1",
            }
        ),
        runtime_factory=factory,
    )
    app = create_app(control)
    app.include_router(create_session_router(manager))
    app.include_router(create_runtime_router(manager, runtimes))
    return TestClient(app), runtimes


def test_comment_route_requires_live_stream() -> None:
    """开播前与缺少操作人时拒绝评论注入。"""
    client, runtimes = _build_app()
    headers = {"x-operator-id": "op_1"}
    with client:
        created = client.post(
            "/v1/streams",
            json={
                "persona_id": "p_1",
                "persona_version": 1,
                "prompt_digest": "d_1",
                "voice_id": "voice_1",
                "avatar_id": "avatar_1",
            },
            headers=headers,
        )
        assert created.status_code == 201
        stream_id = created.json()["stream_id"]
        early = client.post(
            f"/v1/streams/{stream_id}/comments",
            json={"text": "在吗"},
            headers=headers,
        )
        assert early.status_code == 409
        started = client.post(
            f"/v1/streams/{stream_id}/start",
            json={},
            headers=headers,
        )
        assert started.status_code == 200
        missing_operator = client.post(
            f"/v1/streams/{stream_id}/comments",
            json={"text": "在吗"},
        )
        assert missing_operator.status_code == 401
        accepted = client.post(
            f"/v1/streams/{stream_id}/comments",
            json={"text": "在吗", "item_id": "c1"},
            headers=headers,
        )
        assert accepted.status_code == 202
        assert accepted.json() == {
            "accepted": True,
            "outcome": "accepted",
        }
    runtimes.close_all()


def test_placeholder_voice_is_rejected() -> None:
    """场次与默认音色都是占位值时显式报错，避免悄悄降级为字幕。"""
    with pytest.raises(ValueError, match="音色未配置"):
        make_harness(
            config=_config("replace_with_authorized_voice_id"),
        )


def test_placeholder_voice_falls_back_to_default() -> None:
    """场次音色是占位值时回退到已配置的默认音色。"""
    synth = FakeSynth((_audio(),))
    harness = make_harness(
        config=_config("replace_with_authorized_voice_id"),
        synth=synth,
        default_voice="longfeifei_v2",
    )
    assert harness.runtime.submit_comment("你好") is CommentOutcome.ACCEPTED
    harness.runtime.run_once()
    assert synth.requests
    assert synth.requests[0].voice_id == "longfeifei_v2"


def test_session_voice_wins_over_default() -> None:
    """场次音色合法时优先使用场次音色，不被默认音色覆盖。"""
    synth = FakeSynth((_audio(),))
    harness = make_harness(synth=synth, default_voice="longfeifei_v2")
    assert harness.runtime.submit_comment("你好") is CommentOutcome.ACCEPTED
    harness.runtime.run_once()
    assert synth.requests[0].voice_id == "voice_1"
