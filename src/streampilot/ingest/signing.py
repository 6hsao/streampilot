"""直播平台回调的签名校验与防重放。

签名规范见 docs/API.md 第 2 节：对
``method\\npath\\ntimestamp\\nnonce\\nbody_sha256`` 做 HMAC-SHA256。
"""

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass

from redis import Redis

# 允许的时钟偏差，单位秒
MAX_SKEW_SECONDS = 300
# nonce 去重窗口，单位秒
NONCE_TTL_SECONDS = 600
# 对外统一错误信息，避免向调用方泄露失败原因
GENERIC_ERROR = "signature verification failed"


class SignatureError(Exception):
    """签名校验失败。

    Args:
        reason: 内部排查用原因，不得返回给调用方。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(GENERIC_ERROR)
        self.reason = reason


@dataclass(frozen=True)
class SignatureHeaders:
    """签名相关请求头。

    Attributes:
        signature: 十六进制 HMAC 签名。
        timestamp: 秒级 Unix 时间戳字符串。
        nonce: 一次性随机串。
    """

    signature: str
    timestamp: str
    nonce: str

    @classmethod
    def from_mapping(cls, headers: Mapping[str, str]) -> "SignatureHeaders":
        """从请求头中解析签名三要素。

        Args:
            headers: 请求头映射，键名大小写不敏感。

        Returns:
            解析后的签名请求头。

        Raises:
            SignatureError: 任一字段缺失或为空。
        """
        normalized = {key.lower(): value for key, value in headers.items()}
        values = []
        for name in ("x-signature", "x-timestamp", "x-nonce"):
            value = normalized.get(name, "").strip()
            if not value:
                raise SignatureError(f"missing header {name}")
            values.append(value)
        return cls(signature=values[0], timestamp=values[1], nonce=values[2])


def body_sha256(body: bytes) -> str:
    """计算请求体的 SHA256 十六进制摘要。

    Args:
        body: 原始请求体字节。

    Returns:
        小写十六进制摘要。
    """
    return hashlib.sha256(body).hexdigest()


def canonical_string(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    """拼接待签名原文。

    Args:
        method: HTTP 方法，统一大写。
        path: 不含查询串的请求路径。
        timestamp: 秒级时间戳字符串。
        nonce: 一次性随机串。
        body: 原始请求体。

    Returns:
        以换行分隔的规范化字符串。
    """
    return "\n".join(
        [method.upper(), path, timestamp, nonce, body_sha256(body)]
    )


def sign(secret: bytes, canonical: str) -> str:
    """计算 HMAC-SHA256 十六进制签名。

    Args:
        secret: 共享密钥字节。
        canonical: 待签名原文。

    Returns:
        小写十六进制签名。
    """
    digest = hmac.new(secret, canonical.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


class NonceStore:
    """nonce 去重存储协议。"""

    def remember(self, nonce: str, ttl_seconds: int) -> bool:
        """记录 nonce。

        Args:
            nonce: 一次性随机串。
            ttl_seconds: 记录存活时间。

        Returns:
            首次出现返回 True，重复出现返回 False。
        """
        raise NotImplementedError


class InMemoryNonceStore(NonceStore):
    """进程内 nonce 存储，仅用于单测与本地开发。"""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def remember(self, nonce: str, ttl_seconds: int) -> bool:
        """记录 nonce，过期记录会被惰性清理。"""
        now = time.monotonic()
        expired = [
            key for key, deadline in self._seen.items() if deadline <= now
        ]
        for key in expired:
            del self._seen[key]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now + ttl_seconds
        return True


class RedisNonceStore(NonceStore):
    """基于 Redis SETNX 的跨实例 nonce 存储。

    Args:
        client: Redis 客户端。
        prefix: 键前缀，便于多环境隔离。
    """

    def __init__(self, client: Redis, prefix: str = "ingest:nonce:") -> None:
        self._client = client
        self._prefix = prefix

    def remember(self, nonce: str, ttl_seconds: int) -> bool:
        """通过 SET NX EX 原子写入并判断是否首次出现。"""
        stored = self._client.set(
            f"{self._prefix}{nonce}",
            "1",
            nx=True,
            ex=ttl_seconds,
        )
        return bool(stored)


def verify_request(
    secret: bytes,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    nonce_store: NonceStore,
    *,
    now: float | None = None,
) -> None:
    """校验签名、时间窗与 nonce 唯一性。

    Args:
        secret: 共享密钥字节。
        method: HTTP 方法。
        path: 不含查询串的请求路径。
        headers: 请求头映射。
        body: 原始请求体。
        nonce_store: nonce 去重存储。
        now: 当前时间戳，默认取系统时间，便于测试注入。

    Raises:
        SignatureError: 任一项校验失败，对外信息统一。
    """
    parsed = SignatureHeaders.from_mapping(headers)
    try:
        timestamp = int(parsed.timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp is not an integer") from exc
    current = time.time() if now is None else now
    if abs(current - timestamp) > MAX_SKEW_SECONDS:
        raise SignatureError("timestamp outside allowed window")

    canonical = canonical_string(
        method, path, parsed.timestamp, parsed.nonce, body
    )
    expected = sign(secret, canonical)
    if not hmac.compare_digest(expected, parsed.signature.lower()):
        raise SignatureError("signature mismatch")

    if not nonce_store.remember(parsed.nonce, NONCE_TTL_SECONDS):
        raise SignatureError("nonce already used")
