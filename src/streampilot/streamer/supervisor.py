"""推流进程监管：启动、停止与健康快照。

进程以独立方式运行（不经 shell），监管者只持有进程句柄；推流地址
仅在启动瞬间存在于参数列表中，不写入日志、不进入健康快照。
断流重连与码率自适应由 LIV-03 在此之上实现。
"""

import logging
import subprocess
import threading
from collections.abc import Sequence
from typing import Protocol

from streampilot.streamer.ffmpeg import InputMode, build_command
from streampilot.streamer.models import (
    EncoderProfile,
    StreamerState,
    StreamTarget,
)

logger = logging.getLogger(__name__)

DEFAULT_STOP_TIMEOUT_S = 5.0


class StreamerError(Exception):
    """推流进程操作失败。"""


class StreamProcess(Protocol):
    """外部进程句柄。"""

    @property
    def pid(self) -> int:
        """进程号。"""
        ...

    def poll(self) -> int | None:
        """非阻塞读取退出码。"""
        ...

    def terminate(self) -> None:
        """请求终止。"""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """等待退出。"""
        ...


class ProcessRunner(Protocol):
    """命令执行器。"""

    def start(self, command: Sequence[str]) -> StreamProcess:
        """启动进程。"""
        ...


class SubprocessRunner:
    """用 ``subprocess`` 启动 FFmpeg（独立进程、不经 shell）。"""

    def start(self, command: Sequence[str]) -> StreamProcess:
        return subprocess.Popen(  # noqa: S603 - 参数列表由本模块构造
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )


class StreamerSupervisor:
    """管理单个推流进程的生命周期。

    Args:
        profile: 编码参数。
        runner: 命令执行器，测试可注入假实现。
        input_mode: 输入来源。
        executable: ffmpeg 可执行文件路径。
        stop_timeout_s: 终止等待时间。
    """

    def __init__(
        self,
        profile: EncoderProfile,
        *,
        runner: ProcessRunner | None = None,
        input_mode: InputMode = InputMode.FRAMES,
        executable: str = "ffmpeg",
        stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    ) -> None:
        self._profile = profile
        self._runner: ProcessRunner = runner or SubprocessRunner()
        self._input_mode = input_mode
        self._executable = executable
        self._stop_timeout_s = stop_timeout_s
        self._process: StreamProcess | None = None
        self._state = StreamerState.IDLE
        self._lock = threading.Lock()

    @property
    def state(self) -> StreamerState:
        """当前状态。"""
        with self._lock:
            return self._state

    @property
    def profile(self) -> EncoderProfile:
        """当前编码参数。"""
        return self._profile

    def start(self, target: StreamTarget) -> int:
        """启动推流进程。

        Args:
            target: 推流目标。

        Returns:
            进程号。

        Raises:
            StreamerError: 已在运行或进程启动失败。
        """
        with self._lock:
            if self._process is not None:
                raise StreamerError("streamer is already running")
            command = build_command(
                self._profile,
                target,
                input_mode=self._input_mode,
                executable=self._executable,
            )
            try:
                process = self._runner.start(command)
            except OSError as exc:
                self._state = StreamerState.FAILED
                raise StreamerError("failed to start ffmpeg") from exc
            self._process = process
            self._state = StreamerState.RUNNING
            logger.info(
                "streamer started pid=%s profile=%sx%s@%s bitrate=%skbps",
                process.pid,
                self._profile.width,
                self._profile.height,
                self._profile.fps,
                self._profile.bitrate_kbps,
            )
            return process.pid

    def stop(self) -> None:
        """终止推流进程并进入 ``stopped`` 状态。"""
        with self._lock:
            process = self._process
            self._process = None
            self._state = StreamerState.STOPPED
        if process is None:
            return
        try:
            process.terminate()
            process.wait(self._stop_timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("streamer process did not stop cleanly")
        logger.info("streamer stopped")

    def health(self) -> dict[str, object]:
        """返回不含密钥的健康快照。

        Returns:
            状态、进程号与编码参数；进程意外退出时状态转为 ``failed``。
        """
        with self._lock:
            process = self._process
            exit_code: int | None = None
            pid: int | None = None
            if process is not None:
                exit_code = process.poll()
                pid = process.pid
                if exit_code is not None:
                    self._process = None
                    self._state = StreamerState.FAILED
                    logger.warning(
                        "streamer exited unexpectedly code=%s", exit_code
                    )
            return {
                "state": self._state.value,
                "pid": pid,
                "exit_code": exit_code,
                "profile": {
                    "width": self._profile.width,
                    "height": self._profile.height,
                    "fps": self._profile.fps,
                    "bitrate_kbps": self._profile.bitrate_kbps,
                },
            }
