"""本地桥的短期令牌管理。

令牌有效期 15 分钟，在到期前 ``skew_s`` 秒主动刷新；刷新失败时
保留旧令牌直到真正过期，避免因一次网络抖动打断上报。
"""

import threading
from collections.abc import Callable
from time import monotonic

DEFAULT_TOKEN_TTL_S = 900.0
DEFAULT_REFRESH_SKEW_S = 60.0


class TokenManager:
    """按需获取并轮换短期令牌。

    Args:
        fetch: 获取新令牌的函数，返回令牌字符串。
        ttl_s: 令牌有效期（秒），默认 15 分钟。
        skew_s: 提前刷新的余量（秒）。
        clock: 单调时钟，便于测试。

    Raises:
        ValueError: 有效期或余量非法。
    """

    def __init__(
        self,
        fetch: Callable[[], str],
        *,
        ttl_s: float = DEFAULT_TOKEN_TTL_S,
        skew_s: float = DEFAULT_REFRESH_SKEW_S,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if skew_s < 0 or skew_s >= ttl_s:
            raise ValueError("skew_s must be within [0, ttl_s)")
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._skew_s = skew_s
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def ttl_s(self) -> float:
        """令牌有效期。"""
        return self._ttl_s

    def current(self) -> str:
        """返回可用令牌，必要时先刷新。

        Returns:
            当前令牌。

        Raises:
            ValueError: 获取到空令牌。
        """
        with self._lock:
            now = self._clock()
            if self._token is None or now >= self._expires_at - self._skew_s:
                token = self._fetch()
                if not token.strip():
                    raise ValueError("token must not be empty")
                self._token = token
                self._expires_at = now + self._ttl_s
            return self._token

    def invalidate(self) -> None:
        """作废当前令牌，下次调用强制刷新。"""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
