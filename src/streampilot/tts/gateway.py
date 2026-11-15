"""TTS 网关：并发额度、失败重试与字符计费。

编排器只依赖 :class:`SpeechSynthesizer` 协议与 :class:`TTSGateway`；
更换供应商或改为自托管时，无需改动编排与录制逻辑。
"""

import threading
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import Protocol

from streampilot.tts.errors import TTSBusy, TTSError, TTSUnavailable
from streampilot.tts.models import AudioChunk, SynthesisRequest

# 首发为单直播间单音色，并发额度按进程计；多租户时按音色扩展
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_ATTEMPTS = 2


class SpeechSynthesizer(Protocol):
    """语音合成适配器协议。"""

    name: str

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        """流式合成一段文本。

        Args:
            request: 合成请求。

        Yields:
            带时间偏移的音频分片。

        Raises:
            TTSError: 上游失败时抛出已分类错误。
        """
        ...


@dataclass
class TTSStats:
    """网关计数。

    Attributes:
        served: 成功完成的合成次数。
        failed: 最终失败的合成次数。
        retries: 重试次数。
        busy: 因并发额度不足被拒绝的次数。
        characters: 已计费字符总数。
    """

    served: int = 0
    failed: int = 0
    retries: int = 0
    busy: int = 0
    characters: int = 0


class CharacterMeter:
    """按场次与音色累计计费字符数。

    线程安全；进程内实现，后续可替换为 Redis 或计费服务。
    """

    def __init__(self) -> None:
        self._by_session: dict[str, int] = {}
        self._by_voice: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(
        self, *, session_id: str, voice_id: str, characters: int
    ) -> None:
        """累加一次计费。

        Args:
            session_id: 场次标识。
            voice_id: 音色标识。
            characters: 本次字符数，非正数忽略。
        """
        if characters <= 0:
            return
        with self._lock:
            self._by_session[session_id] = (
                self._by_session.get(session_id, 0) + characters
            )
            self._by_voice[voice_id] = (
                self._by_voice.get(voice_id, 0) + characters
            )

    def session_total(self, session_id: str) -> int:
        """读取某场次累计字符数。"""
        with self._lock:
            return self._by_session.get(session_id, 0)

    def voice_total(self, voice_id: str) -> int:
        """读取某音色累计字符数。"""
        with self._lock:
            return self._by_voice.get(voice_id, 0)

    def reset(self) -> None:
        """清空累计值，仅用于测试与场次归档。"""
        with self._lock:
            self._by_session.clear()
            self._by_voice.clear()


class TTSGateway:
    """带并发与重试的合成入口。

    首个分片产出前失败会按 ``max_attempts`` 重试；已产出分片后失败
    直接抛出，由编排器丢弃半截音频，避免音画错位。

    Args:
        synthesizer: 合成适配器。
        max_concurrency: 进程内并发合成上限。
        max_attempts: 单次请求的最大尝试次数。
        meter: 字符计费；缺省使用进程内实现。
        stats: 可选的外部指标对象。

    Raises:
        ValueError: 并发或重试参数非法。
    """

    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        meter: CharacterMeter | None = None,
        stats: TTSStats | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._synthesizer = synthesizer
        self._max_attempts = max_attempts
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self.meter = meter or CharacterMeter()
        self.stats = stats or TTSStats()

    def synthesize(
        self, request: SynthesisRequest, *, session_id: str
    ) -> Generator[AudioChunk, None, None]:
        """合成一段文本并在结束后计费。

        Args:
            request: 合成请求。
            session_id: 场次标识，用于计费归集。

        Yields:
            音频分片。

        Raises:
            TTSBusy: 并发额度已满。
            TTSError: 合成失败。
        """
        if not self._slots.acquire(blocking=False):
            self.stats.busy += 1
            raise TTSBusy("tts concurrency limit reached")
        completed = False
        try:
            yield from self._run(request)
            completed = True
        finally:
            self._slots.release()
            if completed:
                self._bill(request, session_id)

    def _bill(self, request: SynthesisRequest, session_id: str) -> None:
        """统计并累加本次合成字符数。"""
        characters = len(request.text)
        self.stats.characters += characters
        self.meter.record(
            session_id=session_id,
            voice_id=request.voice_id,
            characters=characters,
        )

    def _run(
        self, request: SynthesisRequest
    ) -> Generator[AudioChunk, None, None]:
        """按尝试次数执行合成。"""
        last_error: TTSError | None = None
        for attempt in range(self._max_attempts):
            emitted = False
            try:
                for chunk in self._synthesizer.synthesize(request):
                    emitted = True
                    yield chunk
            except TTSError as error:
                if emitted or not error.retryable:
                    self.stats.failed += 1
                    raise
                last_error = error
                if attempt + 1 < self._max_attempts:
                    self.stats.retries += 1
                continue
            self.stats.served += 1
            return
        self.stats.failed += 1
        raise TTSUnavailable(
            f"tts failed after {self._max_attempts} attempts"
        ) from last_error
