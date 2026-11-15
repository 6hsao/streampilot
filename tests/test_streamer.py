"""推流命令、进度解析与进程监管的测试（假进程，不启动 FFmpeg）。"""

import logging
from collections.abc import Sequence

import pytest

from streampilot.streamer import (
    EncoderProfile,
    InputMode,
    StreamerError,
    StreamerState,
    StreamerSupervisor,
    StreamTarget,
    build_command,
    parse_progress,
)
from streampilot.streamer.models import MAX_BITRATE_KBPS

TARGET = StreamTarget(url="rtmp://push.example.com/live", key="k-secret-123")


class _FakeProcess:
    """模拟外部进程句柄。"""

    def __init__(self, pid: int = 4321, exit_code: int | None = None) -> None:
        self._pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.waited: float | None = None

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = timeout
        return 0


class _FakeRunner:
    """记录命令并返回假进程。"""

    def __init__(
        self,
        process: _FakeProcess | None = None,
        error: OSError | None = None,
    ) -> None:
        self.process = process or _FakeProcess()
        self.error = error
        self.commands: list[Sequence[str]] = []

    def start(self, command: Sequence[str]) -> _FakeProcess:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.process


def _state(supervisor: StreamerSupervisor) -> StreamerState:
    """读取状态，避免静态类型收窄影响后续断言。"""
    return supervisor.state


def test_build_command_frames_mode() -> None:
    """管道模式读取原始帧与 PCM，输出到平台地址。"""
    command = build_command(
        EncoderProfile(), TARGET, input_mode=InputMode.FRAMES
    )
    joined = " ".join(command)

    assert command[0] == "ffmpeg"
    assert "-progress pipe:1" in joined
    assert "-f rawvideo" in joined
    assert "-pix_fmt yuv420p" in joined
    assert "-s 1280x720" in joined
    assert "-i pipe:0" in joined
    assert "-f s16le" in joined
    assert "-i pipe:3" in joined
    assert "-b:v 2500k" in joined
    assert "-bufsize 5000k" in joined
    assert "-g 60" in joined
    assert "-c:a aac" in joined
    assert command[-3:-1] == ["-f", "flv"]
    assert command[-1] == TARGET.output_url


def test_build_command_test_source() -> None:
    """冒烟模式用 lavfi 测试图与正弦音，不需要外部管道。"""
    command = build_command(
        EncoderProfile(width=1920, height=1080),
        TARGET,
        input_mode=InputMode.TEST_SOURCE,
    )
    joined = " ".join(command)

    assert "testsrc2=size=1920x1080:rate=30" in joined
    assert "sine=frequency=440:sample_rate=48000" in joined
    assert "pipe:0" not in joined


def test_parse_progress_reads_last_values() -> None:
    """进度解析容忍单位后缀与缺失字段。"""
    lines = [
        "frame=120",
        "fps=30.0",
        "bitrate=2500.0kbits/s",
        "speed=1.02x",
        "drop_frames=3",
        "progress=continue",
    ]

    snapshot = parse_progress(lines)

    assert snapshot.fps == 30.0
    assert snapshot.bitrate_kbps == 2500.0
    assert snapshot.speed == 1.02
    assert snapshot.dropped_frames == 3
    assert parse_progress(["noise"]) == parse_progress([])
    assert parse_progress(["bitrate=abc"]).bitrate_kbps == 0.0


def test_profile_ladder_steps_down_to_floor() -> None:
    """码率阶梯每次降 400kbps，到下限后返回 None。"""
    profile = EncoderProfile(bitrate_kbps=1200)

    lowered = profile.next_lower()

    assert lowered is not None
    assert lowered.bitrate_kbps == 800
    assert lowered.next_lower() is None
    assert EncoderProfile(bitrate_kbps=MAX_BITRATE_KBPS).gop_frames == 60


def test_profile_validation() -> None:
    """编码参数越界直接报错。"""
    with pytest.raises(ValueError):
        EncoderProfile(width=0)
    with pytest.raises(ValueError):
        EncoderProfile(fps=0)
    with pytest.raises(ValueError):
        EncoderProfile(bitrate_kbps=100)
    with pytest.raises(ValueError):
        EncoderProfile(gop_seconds=0)
    with pytest.raises(ValueError):
        EncoderProfile(audio_rate=16000)


def test_target_masks_key() -> None:
    """目标对象的 repr 不含密钥，完整地址仅在属性中。"""
    text = repr(TARGET)

    assert "k-secret-123" not in text
    assert "key='***'" in text
    assert TARGET.output_url.endswith("/k-secret-123")


def test_target_validation() -> None:
    """协议与密钥都要校验。"""
    with pytest.raises(ValueError):
        StreamTarget(url="https://example.com", key="x")
    with pytest.raises(ValueError):
        StreamTarget(url="rtmp://h/live", key=" ")


def test_supervisor_start_and_stop() -> None:
    """启动返回进程号，停止后进入 stopped。"""
    runner = _FakeRunner()
    supervisor = StreamerSupervisor(EncoderProfile(), runner=runner)

    pid = supervisor.start(TARGET)

    assert pid == 4321
    assert _state(supervisor) is StreamerState.RUNNING
    assert len(runner.commands) == 1

    with pytest.raises(StreamerError, match="already running"):
        supervisor.start(TARGET)

    supervisor.stop()

    assert runner.process.terminated is True
    assert runner.process.waited == 5.0
    assert _state(supervisor) is StreamerState.STOPPED
    supervisor.stop()


def test_supervisor_start_failure_marks_failed() -> None:
    """进程启动失败进入 failed 并抛错。"""
    supervisor = StreamerSupervisor(
        EncoderProfile(), runner=_FakeRunner(error=OSError("no ffmpeg"))
    )

    with pytest.raises(StreamerError, match="failed to start"):
        supervisor.start(TARGET)

    assert supervisor.state is StreamerState.FAILED


def test_health_reports_failure_on_exit() -> None:
    """进程意外退出后健康快照为 failed。"""
    runner = _FakeRunner(_FakeProcess(exit_code=1))
    supervisor = StreamerSupervisor(EncoderProfile(), runner=runner)
    supervisor.start(TARGET)

    first = supervisor.health()
    second = supervisor.health()

    assert first["state"] == "failed"
    assert first["exit_code"] == 1
    assert first["pid"] == 4321
    assert second["pid"] is None
    assert second["state"] == "failed"
    assert second["profile"] == {
        "width": 1280,
        "height": 720,
        "fps": 30,
        "bitrate_kbps": 2500,
    }


def test_logs_do_not_leak_stream_key(caplog: pytest.LogCaptureFixture) -> None:
    """启动与停止日志不含密钥。"""
    caplog.set_level(logging.INFO)
    supervisor = StreamerSupervisor(EncoderProfile(), runner=_FakeRunner())

    supervisor.start(TARGET)
    supervisor.stop()

    text = caplog.text
    assert "k-secret-123" not in text
    assert "rtmp://" not in text
