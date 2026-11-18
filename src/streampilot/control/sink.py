"""控制面下游动作接口。

控制面只发出语义指令，不直接操作 TTS、渲染器或推流进程；
运行时实现该协议，单测与本地联调使用日志实现。
"""

import logging
from typing import Protocol


class ControlSink(Protocol):
    """接管控制的下游动作接口。"""

    def silence(self) -> None:
        """立即停止 AI 发声并清空下游缓冲。"""
        ...

    def resume(self) -> None:
        """恢复 AI 发声。"""
        ...

    def set_muted(self, muted: bool) -> None:
        """切换音频静音。"""
        ...

    def switch_persona(self, persona_version_id: str) -> None:
        """切换人设版本。"""
        ...

    def speak_template(self, template_id: str, text: str) -> None:
        """播放操作者触发的预设话术。"""
        ...

    def reconnect_stream(self) -> None:
        """触发推流重连。"""
        ...


class LoggingControlSink:
    """只记录指令的占位实现，供本地联调使用。

    Args:
        logger: 日志记录器，默认使用 streampilot.control。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("streampilot.control")

    def silence(self) -> None:
        """记录静默指令。"""
        self._logger.info("control sink: silence")

    def resume(self) -> None:
        """记录恢复指令。"""
        self._logger.info("control sink: resume")

    def set_muted(self, muted: bool) -> None:
        """记录静音切换。"""
        self._logger.info("control sink: muted=%s", muted)

    def switch_persona(self, persona_version_id: str) -> None:
        """记录人设切换。"""
        self._logger.info(
            "control sink: switch persona %s", persona_version_id
        )

    def speak_template(self, template_id: str, text: str) -> None:
        """记录预设话术播放。"""
        self._logger.info(
            "control sink: speak template %s (%d chars)",
            template_id,
            len(text),
        )

    def reconnect_stream(self) -> None:
        """记录重连指令。"""
        self._logger.info("control sink: reconnect stream")
