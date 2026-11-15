"""本地桥的云端客户端：mTLS + 短期令牌。

只做两件事：上报心跳、上报推流凭据。**桥不具备推流能力**，本模块
不启动任何媒体进程，也不把凭据写入磁盘或日志。
"""

import logging
import uuid
from datetime import datetime
from typing import Any, cast

import httpx

from streampilot.bridge.models import (
    BridgeHeartbeat,
    RtmpCredential,
    format_iso,
)
from streampilot.bridge.tokens import TokenManager
from streampilot.config.redact import redact

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0


class BridgeError(Exception):
    """桥请求失败。"""


class BridgeAuthError(BridgeError):
    """令牌被拒绝或已过期。"""


class BridgeUnavailable(BridgeError):
    """云端不可达或超时。"""


def _parse_time(raw: object) -> datetime | None:
    """解析云端返回的 ISO-8601 时间。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class BridgeClient:
    """本地桥到云端的出站客户端。

    mTLS 客户端证书由部署侧注入到 ``client``；测试可传入
    ``httpx.Client(transport=...)`` 以避免真实网络。

    Args:
        base_url: 云端 API 地址，必须为 https。
        bridge_id: 桥标识。
        tokens: 短期令牌管理。
        client: 可选的外部 HTTP 客户端。
        timeout_s: 单次请求超时。

    Raises:
        ValueError: 地址非 https 或桥标识为空。
    """

    def __init__(
        self,
        base_url: str,
        *,
        bridge_id: str,
        tokens: TokenManager,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("bridge cloud url must use https")
        if not bridge_id.strip():
            raise ValueError("bridge_id must not be empty")
        self._base_url = base_url.rstrip("/")
        self._bridge_id = bridge_id.strip()
        self._tokens = tokens
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        """关闭内部创建的 HTTP 客户端。"""
        if self._owns_client:
            self._client.close()

    def heartbeat(self, beat: BridgeHeartbeat) -> None:
        """上报一次心跳。

        Args:
            beat: 心跳内容。

        Raises:
            BridgeError: 上报失败。
        """
        self._post("/v1/bridge/heartbeat", beat.as_payload())

    def upload_rtmp(
        self, *, stream_id: str, credential: RtmpCredential
    ) -> datetime | None:
        """上报推流凭据（仅上报，不推流）。

        Args:
            stream_id: 场次标识。
            credential: 平台下发的凭据。

        Returns:
            云端确认的过期时间；未返回时为 ``None``。

        Raises:
            BridgeError: 上报失败或云端未存储。
        """
        rtmp: dict[str, object] = {
            "url": credential.url,
            "key": credential.key,
            "key_ref": f"up_{uuid.uuid4().hex}",
        }
        payload: dict[str, object] = {
            "bridge_id": self._bridge_id,
            "stream_id": stream_id,
            "rtmp": rtmp,
        }
        if credential.expires_at is not None:
            payload["expires_at"] = format_iso(credential.expires_at)
        data = self._post("/v1/bridge/rtmp", payload)
        if data.get("stored") is not True:
            raise BridgeError("credential was not stored")
        logger.info("rtmp credential uploaded for stream %s", stream_id)
        return _parse_time(data.get("expires_at"))

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        """带令牌发送 POST 并归一化错误。"""
        headers = {"Authorization": f"Bearer {self._tokens.current()}"}
        url = f"{self._base_url}{path}"
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise BridgeUnavailable("bridge request timed out") from exc
        except httpx.HTTPError as exc:
            detail = redact(str(exc))
            raise BridgeUnavailable(
                f"bridge request failed: {detail}"
            ) from exc
        if response.status_code in (401, 403):
            self._tokens.invalidate()
            raise BridgeAuthError("bridge token rejected")
        if response.status_code >= 400:
            raise BridgeError(f"bridge returned {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise BridgeError("bridge returned invalid json") from exc
        if not isinstance(data, dict):
            raise BridgeError("bridge returned unexpected payload")
        return cast(dict[str, Any], data)


__all__ = [
    "BridgeAuthError",
    "BridgeClient",
    "BridgeError",
    "BridgeUnavailable",
]
