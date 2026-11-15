"""FFmpeg 命令构造与进度解析。

媒体层使用 **LGPL 构建** 的 FFmpeg，以独立进程运行：不链接、不修改、
不静态编译；发布包附 LGPL 声明与替换说明（LIV-09）。
本模块只生成参数列表，不经 shell，也不打印命令（含密钥）。
"""

from collections.abc import Iterable
from enum import StrEnum

from streampilot.streamer.models import (
    EncoderProfile,
    ProgressSnapshot,
    StreamTarget,
)

VIDEO_PIPE = "pipe:0"
AUDIO_PIPE = "pipe:3"
PROGRESS_PIPE = "pipe:1"


class InputMode(StrEnum):
    """输入来源。"""

    # 渲染进程经管道送入原始 YUV 帧与 PCM 音频（多管道仅 Linux）
    FRAMES = "frames"
    # lavfi 测试图与正弦音，用于 staging 冒烟与连通性检查
    TEST_SOURCE = "test_source"


def build_command(
    profile: EncoderProfile,
    target: StreamTarget,
    *,
    input_mode: InputMode = InputMode.FRAMES,
    executable: str = "ffmpeg",
) -> list[str]:
    """构造 FFmpeg 推流命令。

    Args:
        profile: 编码参数。
        target: 推流目标。
        input_mode: 输入来源。
        executable: ffmpeg 可执行文件。

    Returns:
        参数列表；**含推流密钥**，调用方不得记录到日志。
    """
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-progress",
        PROGRESS_PIPE,
    ]
    command.extend(_input_args(profile, input_mode))
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            profile.preset,
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{profile.bitrate_kbps}k",
            "-maxrate",
            f"{profile.bitrate_kbps}k",
            "-bufsize",
            f"{profile.bitrate_kbps * 2}k",
            "-g",
            str(profile.gop_frames),
            "-c:a",
            "aac",
            "-b:a",
            f"{profile.audio_bitrate_kbps}k",
            "-ar",
            str(profile.audio_rate),
            "-ac",
            "1",
            "-f",
            "flv",
            target.output_url,
        ]
    )
    return command


def _input_args(profile: EncoderProfile, input_mode: InputMode) -> list[str]:
    """构造输入侧参数。"""
    size = f"{profile.width}x{profile.height}"
    if input_mode is InputMode.TEST_SOURCE:
        return [
            "-re",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate={profile.fps}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={profile.audio_rate}",
        ]
    return [
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-s",
        size,
        "-r",
        str(profile.fps),
        "-i",
        VIDEO_PIPE,
        "-f",
        "s16le",
        "-ar",
        str(profile.audio_rate),
        "-ac",
        "1",
        "-i",
        AUDIO_PIPE,
    ]


def parse_progress(lines: Iterable[str]) -> ProgressSnapshot:
    """解析 ``-progress`` 输出。

    Args:
        lines: 形如 ``key=value`` 的行。

    Returns:
        快照；缺失字段按 0 处理。
    """
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip()
    return ProgressSnapshot(
        bitrate_kbps=_number(values.get("bitrate")),
        fps=_number(values.get("fps")),
        speed=_number(values.get("speed")),
        dropped_frames=int(_number(values.get("drop_frames"))),
    )


def _number(raw: str | None) -> float:
    """从 ``2500.0kbits/s`` 之类的值中取出数字。"""
    if raw is None:
        return 0.0
    head = raw.split(" ", 1)[0]
    digits = ""
    for char in head:
        if char.isdigit() or char in ".+-":
            digits += char
        else:
            break
    try:
        return float(digits)
    except ValueError:
        return 0.0
