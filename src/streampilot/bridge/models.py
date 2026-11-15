"""本地凭据桥的数据结构。

桥只负责「取到凭据 → 安全上报」，凭据对象在 ``repr`` 中一律打码，
避免误入日志与异常追踪；凭据不落盘、不进入任何持久化结构。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from streampilot.config.redact import redact

BRIDGE_VERSION = "0.1.0"


def format_iso(value: datetime) -> str:
    """把时间转成秒级 UTC ISO-8601 字符串。

    Args:
        value: 待转换时间，无时区按 UTC 解释。

    Returns:
        形如 ``2026-01-01T12:00:00Z`` 的字符串。
    """
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    utc = moment.astimezone(UTC).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, repr=False)
class RtmpCredential:
    """平台下发的推流凭据。

    Attributes:
        url: 推流地址（不含 key 的完整地址或含 key 的平台原样地址）。
        key: 推流密钥。
        expires_at: 平台声明的过期时间，可为空。

    Raises:
        ValueError: 地址协议非法或密钥为空。
    """

    url: str
    key: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.url.startswith(("rtmp://", "rtmps://")):
            raise ValueError("rtmp url must start with rtmp:// or rtmps://")
        if not self.key.strip():
            raise ValueError("stream key must not be empty")

    def __repr__(self) -> str:
        return (
            f"RtmpCredential(url={redact(self.url)!r}, key='***', "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True)
class BridgeHeartbeat:
    """一次心跳上报。

    Attributes:
        bridge_id: 桥标识。
        host_fingerprint: 主机指纹（哈希，不含明文主机信息）。
        companion_running: 直播伴侣是否在前台运行。
        captured_at: 采集时间。
        version: 桥版本。
    """

    bridge_id: str
    host_fingerprint: str
    companion_running: bool
    captured_at: datetime
    version: str = BRIDGE_VERSION

    def as_payload(self) -> dict[str, object]:
        """转为上报体。"""
        return {
            "bridge_id": self.bridge_id,
            "host_fingerprint": self.host_fingerprint,
            "companion_running": self.companion_running,
            "captured_at": format_iso(self.captured_at),
            "version": self.version,
        }
