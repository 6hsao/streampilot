"""推流器的配置、目标与状态模型。

推流地址与密钥只在内存中拼接，``repr`` 一律打码；该对象不得进入
日志、异常追踪或任何持久化结构。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from streampilot.config.redact import redact

MIN_BITRATE_KBPS = 800
MAX_BITRATE_KBPS = 6000
BITRATE_STEP_KBPS = 400

# 允许的音频采样率，避免平台侧重采样引入杂音
ALLOWED_AUDIO_RATES = frozenset({44100, 48000})


class StreamerState(StrEnum):
    """推流进程状态。"""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, repr=False)
class StreamTarget:
    """平台推流目标。

    Attributes:
        url: RTMP 地址（不含密钥）。
        key: 平台下发的推流密钥。

    Raises:
        ValueError: 协议非法或密钥为空。
    """

    url: str
    key: str

    def __post_init__(self) -> None:
        if not self.url.startswith(("rtmp://", "rtmps://")):
            raise ValueError("rtmp url must start with rtmp:// or rtmps://")
        if not self.key.strip():
            raise ValueError("stream key must not be empty")

    def __repr__(self) -> str:
        return f"StreamTarget(url={redact(self.url)!r}, key='***')"

    @property
    def output_url(self) -> str:
        """进程使用的完整地址，**禁止**写入日志。"""
        return f"{self.url.rstrip('/')}/{self.key}"


@dataclass(frozen=True)
class EncoderProfile:
    """编码参数（1080p30 / 720p30 两档基线）。

    Raises:
        ValueError: 分辨率、帧率、码率或音频参数越界。
    """

    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_kbps: int = 2500
    gop_seconds: int = 2
    preset: str = "veryfast"
    audio_rate: int = 48000
    audio_bitrate_kbps: int = 128

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("resolution must be positive")
        if not 1 <= self.fps <= 60:
            raise ValueError("fps must be within 1 and 60")
        if not MIN_BITRATE_KBPS <= self.bitrate_kbps <= MAX_BITRATE_KBPS:
            raise ValueError("bitrate_kbps out of range")
        if not 1 <= self.gop_seconds <= 10:
            raise ValueError("gop_seconds must be within 1 and 10")
        if self.audio_rate not in ALLOWED_AUDIO_RATES:
            raise ValueError("audio_rate must be 44100 or 48000")
        if self.audio_bitrate_kbps <= 0:
            raise ValueError("audio_bitrate_kbps must be positive")

    @property
    def gop_frames(self) -> int:
        """关键帧间隔（帧）。"""
        return self.fps * self.gop_seconds

    def next_lower(self) -> EncoderProfile | None:
        """降一档码率；已到下限返回 ``None``。

        Returns:
            新的档位，或 ``None`` 表示无法再降。
        """
        target = max(MIN_BITRATE_KBPS, self.bitrate_kbps - BITRATE_STEP_KBPS)
        if target >= self.bitrate_kbps:
            return None
        return replace(self, bitrate_kbps=target)

    def next_higher(
        self, ceiling_kbps: int | None = None
    ) -> EncoderProfile | None:
        """升一档码率，不超过基准或全局上限。

        Args:
            ceiling_kbps: 允许回补到的上限，缺省为全局上限。

        Returns:
            新的档位，或 ``None`` 表示已到上限。
        """
        ceiling = MAX_BITRATE_KBPS
        if ceiling_kbps is not None:
            ceiling = min(ceiling, ceiling_kbps)
        target = min(ceiling, self.bitrate_kbps + BITRATE_STEP_KBPS)
        if target <= self.bitrate_kbps:
            return None
        return replace(self, bitrate_kbps=target)


@dataclass(frozen=True)
class ProgressSnapshot:
    """FFmpeg 进度快照。

    Attributes:
        bitrate_kbps: 实际码率。
        fps: 实际帧率。
        speed: 编码速度倍率。
        dropped_frames: 丢帧数。
    """

    bitrate_kbps: float = 0.0
    fps: float = 0.0
    speed: float = 0.0
    dropped_frames: int = 0
