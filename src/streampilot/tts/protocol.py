"""百炼 CosyVoice WebSocket 协议的消息构造与解析。

只做协议层的纯函数：不建连接、不发请求，便于单测覆盖字段与
错误映射；连接与超时由 :mod:`streampilot.tts.cosyvoice` 负责。
"""

import base64
import binascii
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from streampilot.tts.models import AudioFormat

# 客户端动作
RUN_TASK = "run-task"
CONTINUE_TASK = "continue-task"
FINISH_TASK = "finish-task"

# 服务端事件
TASK_STARTED = "task-started"
RESULT_GENERATED = "result-generated"
TASK_FINISHED = "task-finished"
TASK_FAILED = "task-failed"

TASK_GROUP = "audio"
TASK_NAME = "tts"
FUNCTION = "SpeechSynthesizer"
TEXT_TYPE = "PlainText"


class TaskEventKind(StrEnum):
    """服务端事件归类。"""

    STARTED = "started"
    AUDIO = "audio"
    FINISHED = "finished"
    FAILED = "failed"
    IGNORED = "ignored"


@dataclass(frozen=True)
class TaskEvent:
    """解析后的服务端事件。

    Attributes:
        kind: 事件归类。
        audio: 解码后的 PCM 字节，仅 ``AUDIO`` 事件非空。
        error_code: 上游错误码，仅 ``FAILED`` 事件存在。
        error_message: 上游错误描述，仅 ``FAILED`` 事件存在。
    """

    kind: TaskEventKind
    audio: bytes = b""
    error_code: str | None = None
    error_message: str | None = None


def _header(task_id: str, action: str) -> dict[str, Any]:
    """构造客户端消息头。"""
    return {"action": action, "task_id": task_id, "streaming": "duplex"}


def build_run_task(
    task_id: str,
    *,
    model: str,
    voice_id: str,
    audio_format: AudioFormat,
    speech_rate: float = 1.0,
) -> dict[str, Any]:
    """构造 ``run-task`` 消息。

    Args:
        task_id: 本次合成任务标识。
        model: 模型名称，如 ``cosyvoice-v2``。
        voice_id: 已授权音色标识。
        audio_format: 输出音频格式。
        speech_rate: 语速倍率。

    Returns:
        可直接 ``json.dumps`` 的消息体。
    """
    return {
        "header": _header(task_id, RUN_TASK),
        "payload": {
            "task_group": TASK_GROUP,
            "task": TASK_NAME,
            "function": FUNCTION,
            "model": model,
            "parameters": {
                "text_type": TEXT_TYPE,
                "voice": voice_id,
                "format": audio_format.provider_format,
                "sample_rate": audio_format.sample_rate,
                "rate": speech_rate,
            },
            "input": {},
        },
    }


def build_continue_task(task_id: str, text: str) -> dict[str, Any]:
    """构造 ``continue-task`` 消息，携带待合成文本。"""
    return {
        "header": _header(task_id, CONTINUE_TASK),
        "payload": {"input": {"text": text}},
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    """构造 ``finish-task`` 消息，声明文本已发送完毕。"""
    return {"header": _header(task_id, FINISH_TASK), "payload": {"input": {}}}


def parse_event(raw: str) -> TaskEvent:
    """解析一条服务端消息。

    Args:
        raw: WebSocket 文本帧。

    Returns:
        归类后的事件；未知事件返回 ``IGNORED``。

    Raises:
        ValueError: 非 JSON 对象，或音频字段不是合法 base64。
    """
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("event must be a json object")
    message = cast(dict[str, Any], data)
    header = cast(dict[str, Any], message.get("header") or {})
    event = str(header.get("event") or "")
    if event == TASK_STARTED:
        return TaskEvent(kind=TaskEventKind.STARTED)
    if event == TASK_FINISHED:
        return TaskEvent(kind=TaskEventKind.FINISHED)
    if event == TASK_FAILED:
        return TaskEvent(
            kind=TaskEventKind.FAILED,
            error_code=_optional_str(header.get("error_code")),
            error_message=_optional_str(header.get("error_message")),
        )
    if event == RESULT_GENERATED:
        return TaskEvent(
            kind=TaskEventKind.AUDIO, audio=_decode_audio(message)
        )
    return TaskEvent(kind=TaskEventKind.IGNORED)


def _optional_str(value: object) -> str | None:
    """把可能缺失的字段转成字符串或 None。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decode_audio(message: dict[str, Any]) -> bytes:
    """从 ``result-generated`` 中解出 PCM 字节。"""
    payload = cast(dict[str, Any], message.get("payload") or {})
    output = cast(dict[str, Any], payload.get("output") or {})
    audio = cast(dict[str, Any], output.get("audio") or {})
    encoded = _optional_str(audio.get("data"))
    if encoded is None:
        return b""
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid audio payload") from exc
