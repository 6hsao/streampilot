"""语音合成的数据结构与音频格式。

格式统一为 16 bit 单声道 PCM：时长可由字节数精确推算，便于
唇形驱动按分片时长对齐（AVT-04）。
"""

from dataclasses import dataclass
from enum import StrEnum

# 16 bit 单声道 PCM 每个采样点的字节数
BYTES_PER_SAMPLE = 2


class AudioFormat(StrEnum):
    """受支持的合成音频格式。"""

    PCM_16K_MONO = "pcm_16k_mono"
    PCM_24K_MONO = "pcm_24k_mono"

    @property
    def sample_rate(self) -> int:
        """采样率（Hz）。"""
        return _SAMPLE_RATES[self]

    @property
    def provider_format(self) -> str:
        """上游协议中的容器格式名。"""
        return "pcm"


_SAMPLE_RATES: dict[AudioFormat, int] = {
    AudioFormat.PCM_16K_MONO: 16000,
    AudioFormat.PCM_24K_MONO: 24000,
}


def pcm_duration_ms(nbytes: int, sample_rate: int) -> int:
    """按字节数推算 PCM 音频时长。

    Args:
        nbytes: PCM 字节数。
        sample_rate: 采样率（Hz）。

    Returns:
        时长（毫秒，四舍五入）；非法输入返回 0。

    Raises:
        ValueError: 采样率非正数。
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if nbytes <= 0:
        return 0
    return round(nbytes * 1000 / (BYTES_PER_SAMPLE * sample_rate))


@dataclass(frozen=True)
class SynthesisRequest:
    """一次合成请求。

    Attributes:
        text: 已规范化的待合成文本。
        voice_id: 已授权音色标识。
        audio_format: 输出音频格式。
        speech_rate: 语速倍率，0.5 ~ 2.0。

    Raises:
        ValueError: 文本或音色为空，或语速越界。
    """

    text: str
    voice_id: str
    audio_format: AudioFormat = AudioFormat.PCM_16K_MONO
    speech_rate: float = 1.0

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if not self.voice_id.strip():
            raise ValueError("voice_id must not be empty")
        if not 0.5 <= self.speech_rate <= 2.0:
            raise ValueError("speech_rate must be within 0.5 and 2.0")


@dataclass(frozen=True)
class AudioChunk:
    """一个可播放的音频分片。

    Attributes:
        seq: 分片序号，从 0 开始。
        data: PCM 字节。
        duration_ms: 分片时长（毫秒）。
        start_ms: 相对整段音频的起始偏移（毫秒）。
        sample_rate: 采样率（Hz）。

    Raises:
        ValueError: 序号为负、数据为空或时长非正。
    """

    seq: int
    data: bytes
    duration_ms: int
    start_ms: int
    sample_rate: int

    def __post_init__(self) -> None:
        if self.seq < 0:
            raise ValueError("seq must not be negative")
        if not self.data:
            raise ValueError("audio chunk must not be empty")
        if self.duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        if self.start_ms < 0:
            raise ValueError("start_ms must not be negative")
