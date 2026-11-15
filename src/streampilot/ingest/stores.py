"""接入网关依赖的幂等、限流与事件投递实现。"""

import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from redis import Redis

from streampilot.contracts import IngestEvent

# 内部事件流名称
STREAM_NAME = "ingest.events"
# 事件流近似长度上限，避免无限增长
STREAM_MAXLEN = 100_000


class IdempotencyStore(Protocol):
    """事件幂等存储协议。"""

    def claim(self, key: str, ttl_seconds: int) -> bool:
        """尝试占用幂等键。

        Args:
            key: 幂等键。
            ttl_seconds: 占用有效期。

        Returns:
            首次占用返回 True，重复返回 False。
        """
        ...


class RateLimiter(Protocol):
    """按房间的窗口限流协议。"""

    def allow(self, key: str) -> bool:
        """判断当前请求是否放行。

        Args:
            key: 限流维度键，如房间引用。

        Returns:
            放行返回 True，超限返回 False。
        """
        ...


class EventPublisher(Protocol):
    """内部事件投递协议。"""

    def publish(self, event: IngestEvent, trace_id: str) -> None:
        """投递事件。

        Args:
            event: 已校验的接入事件。
            trace_id: 追踪 ID。
        """
        ...


class InMemoryIdempotencyStore:
    """进程内幂等存储，用于单测与本地开发。"""

    def __init__(self) -> None:
        self._deadlines: dict[str, float] = {}

    def claim(self, key: str, ttl_seconds: int) -> bool:
        """占用幂等键，过期记录会被惰性清理。"""
        now = time.monotonic()
        expired = [
            item
            for item, deadline in self._deadlines.items()
            if deadline <= now
        ]
        for item in expired:
            del self._deadlines[item]
        if key in self._deadlines:
            return False
        self._deadlines[key] = now + ttl_seconds
        return True


class RedisIdempotencyStore:
    """基于 Redis SETNX 的跨实例幂等存储。

    Args:
        client: Redis 客户端。
        prefix: 键前缀。
    """

    def __init__(self, client: Redis, prefix: str = "ingest:idem:") -> None:
        self._client = client
        self._prefix = prefix

    def claim(self, key: str, ttl_seconds: int) -> bool:
        """通过 SET NX EX 原子占用幂等键。"""
        return bool(
            self._client.set(
                f"{self._prefix}{key}", "1", nx=True, ex=ttl_seconds
            )
        )


class InMemoryRateLimiter:
    """固定窗口限流的进程内实现，用于单测。

    Args:
        limit: 每秒允许的请求数。
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._counters: dict[tuple[str, int], int] = {}

    def allow(self, key: str) -> bool:
        """按秒级窗口计数并判断是否放行。"""
        bucket = int(time.time())
        counter_key = (key, bucket)
        current = self._counters.get(counter_key, 0) + 1
        self._counters[counter_key] = current
        if len(self._counters) > 10_000:
            self._counters = {
                item: value
                for item, value in self._counters.items()
                if item[1] >= bucket
            }
        return current <= self._limit


class RedisRateLimiter:
    """基于 Redis INCR 的固定窗口限流。

    Args:
        client: Redis 客户端。
        limit: 每秒允许的请求数。
        prefix: 键前缀。
    """

    def __init__(
        self, client: Redis, limit: int, prefix: str = "ingest:rl:"
    ) -> None:
        self._client = client
        self._limit = limit
        self._prefix = prefix

    def allow(self, key: str) -> bool:
        """按秒级窗口计数并判断是否放行。"""
        bucket = int(time.time())
        counter_key = f"{self._prefix}{key}:{bucket}"
        raw_count = self._client.incr(counter_key)
        if isinstance(raw_count, Awaitable):
            raise RuntimeError("async redis client is not supported")
        count = int(cast(Any, raw_count))
        if count == 1:
            self._client.expire(counter_key, 2)
        return count <= self._limit


@dataclass
class InMemoryEventPublisher:
    """进程内事件收集器，用于单测断言投递次数。"""

    published: list[tuple[IngestEvent, str]] = field(default_factory=list)

    def publish(self, event: IngestEvent, trace_id: str) -> None:
        """记录事件，不产生外部副作用。"""
        self.published.append((event, trace_id))


class RedisStreamPublisher:
    """把接入事件写入 Redis Stream 的投递实现。

    Args:
        client: Redis 客户端。
        stream: 目标流名称。
        maxlen: 流的近似长度上限。
    """

    def __init__(
        self,
        client: Redis,
        stream: str = STREAM_NAME,
        maxlen: int = STREAM_MAXLEN,
    ) -> None:
        self._client = client
        self._stream = stream
        self._maxlen = maxlen

    def publish(self, event: IngestEvent, trace_id: str) -> None:
        """以 XADD 写入事件与追踪 ID。"""
        self._client.xadd(
            self._stream,
            {
                "event": json.dumps(
                    event.model_dump(mode="json"), ensure_ascii=False
                ),
                "trace_id": trace_id,
            },
            maxlen=self._maxlen,
            approximate=True,
        )
