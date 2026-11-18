"""CosyVoice 流式合成（阿里云百炼 WebSocket）。

一次合成 = 建立任务通道 → ``run-task`` → ``continue-task``（文本）→
``finish-task`` → 持续读取音频分片。连接失败、分片超时与上游错误
都映射为 :mod:`streampilot.tts.errors` 中的已分类错误，由网关决定
是否重试或降级为字幕模式。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from streampilot.tts.errors import (
    TTSBadRequest,
    TTSTimeout,
    TTSUnavailable,
    tts_error_from_code,
)
from streampilot.tts.models import (
    AudioChunk,
    SynthesisRequest,
    pcm_duration_ms,
)
from streampilot.tts.protocol import (
    TaskEvent,
    TaskEventKind,
    build_continue_task,
    build_finish_task,
    build_run_task,
    parse_event,
)

DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_TTS_MODEL = "cosyvoice-v2"
# 建立连接、发送文本与等待首个音频分片的上限
DEFAULT_TIMEOUT_S = 10.0
# 等待 task-started 前最多容忍的事件数，防止服务端刷非预期事件
_MAX_PREAMBLE_EVENTS = 3


class TaskTransport(Protocol):
    """一次合成的任务通道。

    文本帧返回 ``str``；``format=pcm`` 时服务端会直接下发二进制
    音频帧，此时返回 ``bytes``。实现方在收发失败时应抛出
    :class:`TTSError` 子类。
    """

    def send(self, payload: str) -> None:
        """发送一条文本消息。"""
        ...

    def recv(self) -> str | bytes:
        """读取一帧：JSON 文本或二进制 PCM。"""
        ...

    def close(self) -> None:
        """关闭通道，可重复调用。"""
        ...


TransportFactory = Callable[[str, dict[str, str], float], TaskTransport]


class WebSocketTransport:
    """把 ``websockets`` 同步连接适配为任务通道。

    Args:
        connection: 已建立的同步连接。
        recv_timeout_s: 单次读取超时。
    """

    def __init__(
        self, connection: Any, *, recv_timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        self._connection = connection
        self._recv_timeout_s = recv_timeout_s

    def send(self, payload: str) -> None:
        try:
            self._connection.send(payload)
        except (ConnectionClosed, WebSocketException, OSError) as exc:
            raise TTSUnavailable("connection closed while sending") from exc

    def recv(self) -> str | bytes:
        try:
            message = self._connection.recv(timeout=self._recv_timeout_s)
        except TimeoutError as exc:
            raise TTSTimeout("timed out waiting for audio") from exc
        except (ConnectionClosed, WebSocketException, OSError) as exc:
            raise TTSUnavailable("connection closed while receiving") from exc
        if isinstance(message, bytes):
            return message
        return str(message)

    def close(self) -> None:
        self._connection.close()


def default_transport_factory(
    url: str, headers: dict[str, str], timeout_s: float
) -> TaskTransport:
    """建立到百炼推理服务的 WebSocket 连接。

    Args:
        url: 完整 wss 地址。
        headers: 含 ``Authorization`` 的握手头。
        timeout_s: 连接与读取超时。

    Returns:
        任务通道。

    Raises:
        TTSTimeout: 连接超时。
        TTSUnavailable: 连接失败。
    """
    try:
        connection = connect(
            url,
            additional_headers=headers,
            open_timeout=timeout_s,
            close_timeout=2.0,
            max_size=None,
        )
    except TimeoutError as exc:
        raise TTSTimeout("timed out connecting to tts service") from exc
    except (OSError, WebSocketException, ValueError) as exc:
        raise TTSUnavailable("failed to connect to tts service") from exc
    return WebSocketTransport(connection, recv_timeout_s=timeout_s)


class CosyVoiceSynthesizer:
    """百炼 CosyVoice 流式合成适配器。

    Args:
        api_key: 百炼 API Key，只从环境变量注入。
        url: 推理服务 WebSocket 地址。
        model: 模型名称。
        transport_factory: 建链函数，便于测试注入。
        timeout_s: 连接与分片等待超时。
        task_id_factory: 任务标识生成器，便于测试固定。

    Raises:
        ValueError: 未提供 API Key。
    """

    name = "cosyvoice"

    def __init__(
        self,
        api_key: str,
        *,
        url: str = DASHSCOPE_WS_URL,
        model: str = DEFAULT_TTS_MODEL,
        transport_factory: TransportFactory = default_transport_factory,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        task_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DASHSCOPE_API_KEY is required")
        self._api_key = api_key.strip()
        self._url = url
        self._model = model
        self._transport_factory = transport_factory
        self._timeout_s = timeout_s
        self._task_id_factory = task_id_factory or (
            lambda: f"tts_{uuid.uuid4().hex}"
        )

    def _open(self) -> TaskTransport:
        """建立任务通道。"""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        return self._transport_factory(self._url, headers, self._timeout_s)

    def synthesize(self, request: SynthesisRequest) -> Iterator[AudioChunk]:
        """流式合成一段文本。

        Args:
            request: 合成请求。

        Yields:
            带时间偏移的音频分片。

        Raises:
            TTSError: 上游失败、鉴权失败或协议异常。
        """
        task_id = self._task_id_factory()
        sample_rate = request.audio_format.sample_rate
        transport = self._open()
        try:
            transport.send(
                _dumps(
                    build_run_task(
                        task_id,
                        model=self._model,
                        voice_id=request.voice_id,
                        audio_format=request.audio_format,
                        speech_rate=request.speech_rate,
                    )
                )
            )
            self._wait_started(transport)
            transport.send(_dumps(build_continue_task(task_id, request.text)))
            transport.send(_dumps(build_finish_task(task_id)))
            yield from self._read_audio(transport, sample_rate)
        finally:
            transport.close()

    @staticmethod
    def _parse_event(raw: str) -> TaskEvent:
        """解析一条文本事件，协议错误转为不可重试错误。"""
        try:
            return parse_event(raw)
        except ValueError as exc:
            raise TTSBadRequest("invalid tts protocol message") from exc

    def _next_event(self, transport: TaskTransport) -> TaskEvent:
        """读取下一条文本事件；启动前出现二进制帧视为协议错误。"""
        raw = transport.recv()
        if isinstance(raw, bytes):
            raise TTSBadRequest("unexpected binary frame before audio")
        return self._parse_event(raw)

    def _wait_started(self, transport: TaskTransport) -> None:
        """等待 ``task-started``，期间出现失败事件即报错。"""
        for _ in range(_MAX_PREAMBLE_EVENTS):
            event = self._next_event(transport)
            if event.kind is TaskEventKind.STARTED:
                return
            if event.kind is TaskEventKind.FAILED:
                raise tts_error_from_code(
                    event.error_code, event.error_message
                )
        raise TTSUnavailable("tts task did not start")

    def _read_audio(
        self, transport: TaskTransport, sample_rate: int
    ) -> Iterator[AudioChunk]:
        """持续读取音频分片直到任务结束。

        ``format=pcm`` 时服务端把音频放在二进制帧里，文本帧只承载
        句子边界与结束事件；两种形状都要处理。
        """
        seq = 0
        offset_ms = 0
        while True:
            raw = transport.recv()
            if isinstance(raw, bytes):
                duration_ms = pcm_duration_ms(len(raw), sample_rate)
                if duration_ms <= 0:
                    continue
                yield AudioChunk(
                    seq=seq,
                    data=raw,
                    duration_ms=duration_ms,
                    start_ms=offset_ms,
                    sample_rate=sample_rate,
                )
                seq += 1
                offset_ms += duration_ms
                continue
            event = self._parse_event(raw)
            if event.kind is TaskEventKind.FAILED:
                raise tts_error_from_code(
                    event.error_code, event.error_message
                )
            if event.kind is TaskEventKind.FINISHED:
                return
            if event.kind is not TaskEventKind.AUDIO or not event.audio:
                continue
            duration_ms = pcm_duration_ms(len(event.audio), sample_rate)
            if duration_ms <= 0:
                continue
            yield AudioChunk(
                seq=seq,
                data=event.audio,
                duration_ms=duration_ms,
                start_ms=offset_ms,
                sample_rate=sample_rate,
            )
            seq += 1
            offset_ms += duration_ms


def _dumps(message: dict[str, Any]) -> str:
    """序列化协议消息，保留中文。"""
    return json.dumps(message, ensure_ascii=False)


__all__ = [
    "DASHSCOPE_WS_URL",
    "DEFAULT_TTS_MODEL",
    "CosyVoiceSynthesizer",
    "TaskTransport",
    "TransportFactory",
    "WebSocketTransport",
    "default_transport_factory",
]
