"""语音合成：文本规范化、CosyVoice 流式接入与合成网关。"""

from streampilot.tts.cosyvoice import (
    DASHSCOPE_WS_URL,
    DEFAULT_TTS_MODEL,
    CosyVoiceSynthesizer,
    TaskTransport,
    WebSocketTransport,
    default_transport_factory,
)
from streampilot.tts.errors import (
    TTSAuthFailed,
    TTSBadRequest,
    TTSBusy,
    TTSError,
    TTSRateLimited,
    TTSTimeout,
    TTSUnavailable,
    tts_error_from_code,
)
from streampilot.tts.gateway import (
    CharacterMeter,
    SpeechSynthesizer,
    TTSGateway,
    TTSStats,
)
from streampilot.tts.models import (
    AudioChunk,
    AudioFormat,
    SynthesisRequest,
    pcm_duration_ms,
)
from streampilot.tts.normalize import (
    int_to_chinese,
    normalize_for_tts,
    strip_emoji,
)

__all__ = [
    "DASHSCOPE_WS_URL",
    "DEFAULT_TTS_MODEL",
    "AudioChunk",
    "AudioFormat",
    "CharacterMeter",
    "CosyVoiceSynthesizer",
    "SpeechSynthesizer",
    "SynthesisRequest",
    "TTSAuthFailed",
    "TTSBadRequest",
    "TTSBusy",
    "TTSError",
    "TTSGateway",
    "TTSRateLimited",
    "TTSStats",
    "TTSTimeout",
    "TTSUnavailable",
    "TaskTransport",
    "WebSocketTransport",
    "default_transport_factory",
    "int_to_chinese",
    "normalize_for_tts",
    "pcm_duration_ms",
    "strip_emoji",
    "tts_error_from_code",
]
