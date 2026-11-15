"""CosyVoice 协议消息构造与解析的测试。"""

import base64
import json

import pytest

from streampilot.tts.errors import (
    TTSAuthFailed,
    TTSBadRequest,
    TTSRateLimited,
    TTSUnavailable,
    tts_error_from_code,
)
from streampilot.tts.models import AudioFormat
from streampilot.tts.protocol import (
    CONTINUE_TASK,
    FINISH_TASK,
    RUN_TASK,
    TaskEventKind,
    build_continue_task,
    build_finish_task,
    build_run_task,
    parse_event,
)


def test_build_run_task_carries_voice_and_format() -> None:
    """run-task 携带音色、采样率与语速。"""
    message = build_run_task(
        "task_1",
        model="cosyvoice-v2",
        voice_id="voice_001",
        audio_format=AudioFormat.PCM_24K_MONO,
        speech_rate=1.2,
    )

    header = message["header"]
    payload = message["payload"]

    assert header["action"] == RUN_TASK
    assert header["task_id"] == "task_1"
    assert header["streaming"] == "duplex"
    assert payload["task_group"] == "audio"
    assert payload["task"] == "tts"
    assert payload["function"] == "SpeechSynthesizer"
    assert payload["model"] == "cosyvoice-v2"
    parameters = payload["parameters"]
    assert parameters["voice"] == "voice_001"
    assert parameters["format"] == "pcm"
    assert parameters["sample_rate"] == 24000
    assert parameters["rate"] == 1.2


def test_build_continue_and_finish_task() -> None:
    """文本在 continue-task，finish-task 只声明结束。"""
    continue_message = build_continue_task("task_1", "你好，欢迎。")
    finish_message = build_finish_task("task_1")

    assert continue_message["header"]["action"] == CONTINUE_TASK
    assert continue_message["payload"]["input"]["text"] == "你好，欢迎。"
    assert finish_message["header"]["action"] == FINISH_TASK
    assert finish_message["payload"]["input"] == {}


def _server_message(header: dict[str, object], **extra: object) -> str:
    """构造服务端消息文本。"""
    body: dict[str, object] = {"header": header}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def test_parse_started_and_finished() -> None:
    """task-started 与 task-finished 分别归类。"""
    started = parse_event(_server_message({"event": "task-started"}))
    finished = parse_event(_server_message({"event": "task-finished"}))

    assert started.kind is TaskEventKind.STARTED
    assert finished.kind is TaskEventKind.FINISHED


def test_parse_audio_decodes_base64() -> None:
    """result-generated 中的音频被解码为原始字节。"""
    raw = b"\x00\x01" * 8
    message = _server_message(
        {"event": "result-generated"},
        payload={
            "output": {"audio": {"data": base64.b64encode(raw).decode()}}
        },
    )

    event = parse_event(message)

    assert event.kind is TaskEventKind.AUDIO
    assert event.audio == raw


def test_parse_audio_without_data_is_empty() -> None:
    """缺少音频字段时返回空字节，由上层跳过。"""
    event = parse_event(
        _server_message({"event": "result-generated"}, payload={"output": {}})
    )

    assert event.kind is TaskEventKind.AUDIO
    assert event.audio == b""


def test_parse_failed_carries_error_detail() -> None:
    """task-failed 带上游错误码与描述。"""
    event = parse_event(
        _server_message(
            {
                "event": "task-failed",
                "error_code": "Throttling.RateQuota",
                "error_message": "requests exceeded",
            }
        )
    )

    assert event.kind is TaskEventKind.FAILED
    assert event.error_code == "Throttling.RateQuota"
    assert event.error_message == "requests exceeded"


def test_parse_unknown_event_is_ignored() -> None:
    """未知事件不视为错误，交由调用方跳过。"""
    event = parse_event(_server_message({"event": "task-unknown"}))

    assert event.kind is TaskEventKind.IGNORED


def test_parse_rejects_broken_payloads() -> None:
    """非 JSON、非对象与非法 base64 都抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_event("not-json")
    with pytest.raises(ValueError):
        parse_event("[1, 2]")
    with pytest.raises(ValueError):
        parse_event(
            _server_message(
                {"event": "result-generated"},
                payload={"output": {"audio": {"data": "***"}}},
            )
        )


def test_error_code_mapping() -> None:
    """错误码按鉴权、限流、参数、兜底四类映射。"""
    assert isinstance(tts_error_from_code("InvalidApiKey"), TTSAuthFailed)
    assert isinstance(
        tts_error_from_code("Throttling.RateQuota"), TTSRateLimited
    )
    assert isinstance(tts_error_from_code("BadRequest"), TTSBadRequest)
    assert isinstance(tts_error_from_code(None), TTSUnavailable)
    assert tts_error_from_code("Unauthorized", "expired").message == "expired"
