"""TTS 网关的并发、重试与计费测试。"""

from collections.abc import Iterator, Sequence
from typing import cast

import pytest

from streampilot.tts.errors import (
    TTSAuthFailed,
    TTSBusy,
    TTSRateLimited,
    TTSUnavailable,
)
from streampilot.tts.gateway import CharacterMeter, TTSGateway
from streampilot.tts.models import AudioChunk, SynthesisRequest


def _chunk(seq: int = 0) -> AudioChunk:
    """构造一个最短音频分片。"""
    return AudioChunk(
        seq=seq, data=b"\x00\x00", duration_ms=1, start_ms=0, sample_rate=16000
    )


def _request(text: str = "欢迎来到直播间。") -> SynthesisRequest:
    return SynthesisRequest(text=text, voice_id="voice_001")


class _ScriptedSynth:
    """按脚本逐次返回分片或抛出异常的假适配器。"""

    name = "scripted"

    def __init__(self, *scripts: Sequence[object]) -> None:
        self._scripts = [list(script) for script in scripts]
        self.calls = 0

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        self.calls += 1
        script = self._scripts.pop(0) if self._scripts else []
        for step in script:
            if isinstance(step, Exception):
                raise step
            yield cast(AudioChunk, step)


def test_successful_synthesis_is_billed() -> None:
    """成功合成后按字符数计费并计入统计。"""
    gateway = TTSGateway(_ScriptedSynth([_chunk(), _chunk(1)]))

    chunks = list(gateway.synthesize(_request(), session_id="ss_1"))

    assert len(chunks) == 2
    assert gateway.stats.served == 1
    assert gateway.stats.characters == len("欢迎来到直播间。")
    assert gateway.meter.session_total("ss_1") == len("欢迎来到直播间。")
    assert gateway.meter.voice_total("voice_001") == len("欢迎来到直播间。")


def test_retries_before_first_chunk() -> None:
    """首个分片前失败会重试，成功后只计一次费。"""
    gateway = TTSGateway(
        _ScriptedSynth([TTSUnavailable()], [_chunk()]),
        max_attempts=2,
    )

    chunks = list(gateway.synthesize(_request(), session_id="ss_1"))

    assert len(chunks) == 1
    assert gateway.stats.retries == 1
    assert gateway.stats.served == 1
    assert gateway.stats.failed == 0


def test_failure_after_chunk_is_not_retried() -> None:
    """已产出分片后失败直接抛出，避免半截音频错位。"""
    gateway = TTSGateway(
        _ScriptedSynth([_chunk(), TTSRateLimited()]), max_attempts=3
    )

    with pytest.raises(TTSRateLimited):
        list(gateway.synthesize(_request(), session_id="ss_1"))

    assert gateway.stats.retries == 0
    assert gateway.stats.failed == 1
    assert gateway.stats.characters == 0


def test_non_retryable_error_stops_immediately() -> None:
    """鉴权类错误不重试。"""
    synth = _ScriptedSynth([TTSAuthFailed()], [_chunk()])
    gateway = TTSGateway(synth, max_attempts=3)

    with pytest.raises(TTSAuthFailed):
        list(gateway.synthesize(_request(), session_id="ss_1"))

    assert synth.calls == 1
    assert gateway.stats.failed == 1


def test_all_attempts_failing_raises_unavailable() -> None:
    """重试耗尽后抛出上游不可用，并保留尝试次数信息。"""
    gateway = TTSGateway(
        _ScriptedSynth([TTSUnavailable()], [TTSUnavailable()]),
        max_attempts=2,
    )

    with pytest.raises(TTSUnavailable, match="after 2 attempts"):
        list(gateway.synthesize(_request(), session_id="ss_1"))

    assert gateway.stats.retries == 1
    assert gateway.stats.failed == 1


def test_concurrency_limit_rejects_and_releases() -> None:
    """额度占满时拒绝新请求，消费结束后释放。"""
    gateway = TTSGateway(
        _ScriptedSynth([_chunk()], [_chunk()]), max_concurrency=1
    )

    first = gateway.synthesize(_request(), session_id="ss_1")
    next(first)

    with pytest.raises(TTSBusy):
        list(gateway.synthesize(_request(), session_id="ss_1"))

    assert gateway.stats.busy == 1

    first.close()
    assert len(list(gateway.synthesize(_request(), session_id="ss_1"))) == 1


def test_unfinished_stream_is_not_billed() -> None:
    """中途关闭的合成不计费。"""
    gateway = TTSGateway(_ScriptedSynth([_chunk(), _chunk(1)]))

    stream = gateway.synthesize(_request(), session_id="ss_1")
    next(stream)
    stream.close()

    assert gateway.stats.characters == 0
    assert gateway.meter.session_total("ss_1") == 0


def test_invalid_gateway_options() -> None:
    """并发与重试参数必须为正。"""
    synth = _ScriptedSynth()

    with pytest.raises(ValueError):
        TTSGateway(synth, max_concurrency=0)
    with pytest.raises(ValueError):
        TTSGateway(synth, max_attempts=0)


def test_meter_ignores_non_positive_and_resets() -> None:
    """非正字符数不计入，reset 清空累计。"""
    meter = CharacterMeter()

    meter.record(session_id="ss_1", voice_id="v_1", characters=0)
    meter.record(session_id="ss_1", voice_id="v_1", characters=5)

    assert meter.session_total("ss_1") == 5
    assert meter.voice_total("v_1") == 5

    meter.reset()
    assert meter.session_total("ss_1") == 0
    assert meter.voice_total("v_1") == 0
