"""CosyVoice 流式合成适配器的测试（假传输，不触网）。"""

import base64
import json
from collections.abc import Sequence
from typing import cast

import pytest

from streampilot.tts import cosyvoice as module
from streampilot.tts.cosyvoice import (
    CosyVoiceSynthesizer,
    TaskTransport,
    TransportFactory,
    WebSocketTransport,
)
from streampilot.tts.errors import (
    TTSAuthFailed,
    TTSBadRequest,
    TTSRateLimited,
    TTSTimeout,
    TTSUnavailable,
)
from streampilot.tts.models import AudioFormat, SynthesisRequest


class _FakeTransport:
    """按脚本返回服务端消息的假通道。"""

    def __init__(self, messages: Sequence[str | bytes | Exception]) -> None:
        self._messages = list(messages)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(cast(dict[str, object], json.loads(payload)))

    def recv(self) -> str | bytes:
        if not self._messages:
            raise TTSUnavailable("no more messages")
        item = self._messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def _factory(
    transport: _FakeTransport, captured: dict[str, object]
) -> TransportFactory:
    """返回固定通道的建链函数，并记录握手参数。"""

    def build(
        url: str, headers: dict[str, str], timeout_s: float
    ) -> TaskTransport:
        captured.update(
            {"url": url, "headers": headers, "timeout_s": timeout_s}
        )
        return transport

    return build


def _event(header: dict[str, object], **extra: object) -> str:
    """构造服务端事件文本。"""
    body: dict[str, object] = {"header": header}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _started() -> str:
    return _event({"event": "task-started"})


def _finished() -> str:
    return _event({"event": "task-finished"})


def _audio(pcm: bytes) -> str:
    return _event(
        {"event": "result-generated"},
        payload={
            "output": {"audio": {"data": base64.b64encode(pcm).decode()}}
        },
    )


def _failed(code: str) -> str:
    return _event({"event": "task-failed", "error_code": code})


def _request(
    *, audio_format: AudioFormat = AudioFormat.PCM_16K_MONO
) -> SynthesisRequest:
    return SynthesisRequest(
        text="欢迎来到直播间。",
        voice_id="voice_001",
        audio_format=audio_format,
    )


def _synthesizer(
    transport: _FakeTransport, captured: dict[str, object]
) -> CosyVoiceSynthesizer:
    return CosyVoiceSynthesizer(
        "test-key",
        transport_factory=_factory(transport, captured),
        task_id_factory=lambda: "task_fixed",
    )


def test_streams_chunks_with_offsets() -> None:
    """分片按时长累加偏移，且消息顺序为 run/continue/finish。"""
    transport = _FakeTransport(
        [
            _started(),
            _audio(b"\x00" * 3200),
            _audio(b"\x00" * 1600),
            _finished(),
        ]
    )
    captured: dict[str, object] = {}
    synthesizer = _synthesizer(transport, captured)

    chunks = list(synthesizer.synthesize(_request()))

    assert [chunk.duration_ms for chunk in chunks] == [100, 50]
    assert [chunk.start_ms for chunk in chunks] == [0, 100]
    assert [chunk.seq for chunk in chunks] == [0, 1]
    assert all(chunk.sample_rate == 16000 for chunk in chunks)
    actions = [
        cast(dict[str, object], item["header"])["action"]
        for item in transport.sent
    ]
    assert actions == ["run-task", "continue-task", "finish-task"]
    assert transport.closed is True
    headers = cast(dict[str, str], captured["headers"])
    assert headers["Authorization"] == "Bearer test-key"


def test_streams_binary_pcm_frames() -> None:
    """format=pcm 时二进制帧按采样率直接换算时长。"""
    transport = _FakeTransport(
        [
            _started(),
            b"\x00" * 320,
            b"\x00" * 160,
            _finished(),
        ]
    )
    synthesizer = _synthesizer(transport, {})

    chunks = list(synthesizer.synthesize(_request()))

    assert [chunk.duration_ms for chunk in chunks] == [10, 5]
    assert [chunk.start_ms for chunk in chunks] == [0, 10]
    assert [chunk.seq for chunk in chunks] == [0, 1]


def test_binary_frame_before_started_is_bad_request() -> None:
    """启动前收到二进制帧说明协议不符，按不可重试错误处理。"""
    transport = _FakeTransport([b"\x00" * 320])
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSBadRequest, match="binary frame"):
        list(synthesizer.synthesize(_request()))


def test_request_options_are_filled_into_run_task() -> None:
    """音色、采样率与文本透传到协议消息。"""
    transport = _FakeTransport([_started(), _finished()])
    captured: dict[str, object] = {}
    synthesizer = _synthesizer(transport, captured)

    list(
        synthesizer.synthesize(
            SynthesisRequest(
                text="试听一句。",
                voice_id="voice_002",
                audio_format=AudioFormat.PCM_24K_MONO,
                speech_rate=1.5,
            )
        )
    )

    run_task = transport.sent[0]
    header = cast(dict[str, object], run_task["header"])
    payload = cast(dict[str, object], run_task["payload"])
    parameters = cast(dict[str, object], payload["parameters"])
    assert header["task_id"] == "task_fixed"
    assert parameters["voice"] == "voice_002"
    assert parameters["sample_rate"] == 24000
    assert parameters["rate"] == 1.5
    continue_task = transport.sent[1]
    sent_payload = cast(dict[str, object], continue_task["payload"])
    assert (
        cast(dict[str, object], sent_payload["input"])["text"] == "试听一句。"
    )


def test_task_failed_before_start_maps_error() -> None:
    """启动前失败按错误码映射，并关闭通道。"""
    transport = _FakeTransport([_failed("InvalidApiKey")])
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSAuthFailed):
        list(synthesizer.synthesize(_request()))

    assert transport.closed is True


def test_task_failed_mid_stream_maps_error() -> None:
    """已产出分片后失败直接抛出，不重放。"""
    transport = _FakeTransport(
        [_started(), _audio(b"\x00" * 320), _failed("Throttling.RateQuota")]
    )
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSRateLimited):
        list(synthesizer.synthesize(_request()))


def test_transport_error_propagates() -> None:
    """通道异常原样上抛，关闭仍会执行。"""
    transport = _FakeTransport([_started(), TTSUnavailable("closed")])
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSUnavailable):
        list(synthesizer.synthesize(_request()))

    assert transport.closed is True


def test_invalid_message_is_bad_request() -> None:
    """无法解析的消息归类为不可重试错误。"""
    transport = _FakeTransport(["not-json"])
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSBadRequest):
        list(synthesizer.synthesize(_request()))


def test_task_must_start_within_limit() -> None:
    """长时间收不到 task-started 视为上游不可用。"""
    ignored = _event({"event": "task-unknown"})
    transport = _FakeTransport([ignored, ignored, ignored])
    synthesizer = _synthesizer(transport, {})

    with pytest.raises(TTSUnavailable, match="did not start"):
        list(synthesizer.synthesize(_request()))


def test_api_key_is_required() -> None:
    """缺少 API Key 直接报错，避免发出匿名请求。"""
    with pytest.raises(ValueError):
        CosyVoiceSynthesizer("  ")


def test_module_exports_default_factory() -> None:
    """默认建链函数可被替换，模块保持可导入。"""
    assert callable(module.default_transport_factory)


class _FakeConnection:
    """模拟 websockets 同步连接。"""

    def __init__(self, message: object = "ok") -> None:
        self.message = message
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        if isinstance(self.message, Exception) and not self.sent:
            raise self.message
        self.sent.append(payload)

    def recv(self, timeout: float | None = None) -> object:
        if isinstance(self.message, Exception):
            raise self.message
        return self.message

    def close(self) -> None:
        self.closed = True


def test_websocket_transport_roundtrip() -> None:
    """文本原样返回，二进制帧保持字节不做解码。"""
    connection = _FakeConnection("hello")
    transport = WebSocketTransport(connection)

    transport.send("ping")

    assert connection.sent == ["ping"]
    assert transport.recv() == "hello"
    assert WebSocketTransport(_FakeConnection(b"{}")).recv() == b"{}"


def test_websocket_transport_maps_failures() -> None:
    """超时与断连映射为可重试错误。"""
    with pytest.raises(TTSTimeout):
        WebSocketTransport(_FakeConnection(TimeoutError())).recv()
    with pytest.raises(TTSUnavailable):
        WebSocketTransport(_FakeConnection(OSError("boom"))).recv()
    with pytest.raises(TTSUnavailable):
        WebSocketTransport(_FakeConnection(OSError("boom"))).send("ping")


def test_websocket_transport_closes_connection() -> None:
    """关闭透传到连接。"""
    connection = _FakeConnection()
    transport = WebSocketTransport(connection)

    transport.close()

    assert connection.closed is True
