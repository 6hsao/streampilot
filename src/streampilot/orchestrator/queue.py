"""发言队列：优先级插队、冷却、过期丢弃与容量上限。

单场次一个队列，由编排器单写者独占出队；不同场次互不影响。
时间统一由调用方以毫秒时间戳传入，便于测试复现。
"""

import heapq
import itertools
import json
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Protocol, cast

from redis import Redis

# 高优先级越过的时长窗口；超出窗口的旧消息仍会先被消费，避免饥饿
PRIORITY_WINDOW_MS = 30_000
# 默认队列容量
DEFAULT_MAX_SIZE = 50
# 两次发声之间的最小间隔
DEFAULT_COOLDOWN_MS = 2_000
# 消息在队列中的最长存活时间，超过即视为过期
DEFAULT_TTL_MS = 60_000


class Priority(IntEnum):
    """发言优先级。"""

    NORMAL = 0
    HIGH = 1
    URGENT = 2


class EnqueueResult(StrEnum):
    """入队结果。"""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED_FULL = "rejected_full"


@dataclass(frozen=True)
class QueuedTurn:
    """待发声的一轮发言。

    Attributes:
        item_id: 评论或消息唯一标识，用于去重。
        text: 待回复的评论文本。
        enqueued_at_ms: 入队时间，毫秒时间戳。
        expires_at_ms: 过期时间，超过后不再发声。
        priority: 发言优先级。
    """

    item_id: str
    text: str
    enqueued_at_ms: int
    expires_at_ms: int
    priority: Priority = Priority.NORMAL

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id must not be empty")
        if self.expires_at_ms <= self.enqueued_at_ms:
            raise ValueError("expires_at_ms must be later than enqueued_at_ms")

    @property
    def score(self) -> int:
        """队列排序分值，越小越先发声。"""
        return self.enqueued_at_ms - int(self.priority) * PRIORITY_WINDOW_MS

    def dumps(self) -> str:
        """序列化为 Redis 成员值。"""
        return json.dumps(
            {
                "item_id": self.item_id,
                "text": self.text,
                "enqueued_at_ms": self.enqueued_at_ms,
                "expires_at_ms": self.expires_at_ms,
                "priority": int(self.priority),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: str) -> "QueuedTurn":
        """从 Redis 成员值反序列化。

        Args:
            raw: 成员 JSON 文本。

        Returns:
            反序列化后的发言。
        """
        data = cast(dict[str, Any], json.loads(raw))
        return cls(
            item_id=str(data["item_id"]),
            text=str(data["text"]),
            enqueued_at_ms=int(data["enqueued_at_ms"]),
            expires_at_ms=int(data["expires_at_ms"]),
            priority=Priority(int(data["priority"])),
        )


@dataclass
class QueueStats:
    """队列运行指标。

    Attributes:
        size: 当前队列长度。
        enqueued: 累计入队数。
        dequeued: 累计出队数。
        dropped_expired: 因过期被丢弃的数量。
        evicted: 因容量上限被淘汰的数量。
    """

    size: int = 0
    enqueued: int = 0
    dequeued: int = 0
    dropped_expired: int = 0
    evicted: int = 0


class SpeechQueue(Protocol):
    """发言队列协议。"""

    def enqueue(self, turn: QueuedTurn) -> EnqueueResult:
        """把待发言加入队列。

        Args:
            turn: 待发言。

        Returns:
            入队结果。
        """
        ...

    def dequeue(self, now_ms: int) -> QueuedTurn | None:
        """按优先级取出下一条可发声的发言。

        Args:
            now_ms: 当前时间，毫秒时间戳。

        Returns:
            可发声的发言；冷却中、队列为空或全部过期时返回 None。
        """
        ...

    def stats(self) -> QueueStats:
        """返回当前队列指标。"""
        ...


class _QueueBase:
    """队列公共逻辑：参数校验、冷却与指标计数。

    Args:
        max_size: 队列容量上限。
        cooldown_ms: 两次发声的最小间隔。
        ttl_ms: 消息最长存活时间。
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        ttl_ms: int = DEFAULT_TTL_MS,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be positive")
        if cooldown_ms < 0:
            raise ValueError("cooldown_ms must not be negative")
        if ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")
        self.max_size = max_size
        self.cooldown_ms = cooldown_ms
        self.ttl_ms = ttl_ms
        self._counters = QueueStats()

    def _bump(self, name: str, amount: int = 1) -> None:
        """累加计数。"""
        setattr(self._counters, name, getattr(self._counters, name) + amount)

    def make_turn(
        self,
        item_id: str,
        text: str,
        now_ms: int,
        priority: Priority = Priority.NORMAL,
    ) -> QueuedTurn:
        """按队列 TTL 构造待发言。

        Args:
            item_id: 评论或消息唯一标识。
            text: 待回复的评论文本。
            now_ms: 入队时间，毫秒时间戳。
            priority: 发言优先级。

        Returns:
            过期时间已按 TTL 填好的待发言。
        """
        return QueuedTurn(
            item_id=item_id,
            text=text,
            enqueued_at_ms=now_ms,
            expires_at_ms=now_ms + self.ttl_ms,
            priority=priority,
        )

    def stats(self) -> QueueStats:
        """返回队列指标快照。"""
        return QueueStats(**vars(self._counters))


class InMemorySpeechQueue(_QueueBase):
    """进程内发言队列，用于单测与本地开发。"""

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        ttl_ms: int = DEFAULT_TTL_MS,
    ) -> None:
        super().__init__(max_size, cooldown_ms, ttl_ms)
        self._turns: dict[str, QueuedTurn] = {}
        self._heap: list[tuple[int, int, str]] = []
        self._seq = itertools.count()
        self._cooldown_until_ms = 0

    def enqueue(self, turn: QueuedTurn) -> EnqueueResult:
        """入队；超容量时淘汰分值最差的一条。"""
        if turn.item_id in self._turns:
            return EnqueueResult.DUPLICATE
        self._turns[turn.item_id] = turn
        heapq.heappush(self._heap, (turn.score, next(self._seq), turn.item_id))
        self._bump("enqueued")
        return self._enforce_capacity(turn.item_id)

    def _enforce_capacity(self, newest: str) -> EnqueueResult:
        """超出容量时淘汰最差项，若最差项是新入队项则视为拒绝。"""
        while len(self._turns) > self.max_size:
            worst_id = max(
                self._turns, key=lambda item: self._turns[item].score
            )
            del self._turns[worst_id]
            if worst_id == newest:
                return EnqueueResult.REJECTED_FULL
            self._bump("evicted")
        return EnqueueResult.ACCEPTED

    def dequeue(self, now_ms: int) -> QueuedTurn | None:
        """按优先级出队，跳过已过期项，并进入冷却。"""
        if now_ms < self._cooldown_until_ms:
            return None
        while self._heap:
            _, _, item_id = self._heap[0]
            turn = self._turns.get(item_id)
            if turn is None:
                heapq.heappop(self._heap)
                continue
            if turn.expires_at_ms <= now_ms:
                heapq.heappop(self._heap)
                del self._turns[item_id]
                self._bump("dropped_expired")
                continue
            heapq.heappop(self._heap)
            del self._turns[item_id]
            self._bump("dequeued")
            self._cooldown_until_ms = now_ms + self.cooldown_ms
            return turn
        return None

    def stats(self) -> QueueStats:
        """返回队列指标快照，长度取实时存量。"""
        snapshot = super().stats()
        snapshot.size = len(self._turns)
        return snapshot


class RedisSpeechQueue(_QueueBase):
    """基于 Redis ZSet 的发言队列，供多实例共享单场次队列。

    Args:
        client: Redis 客户端。
        session_id: 场次标识。
        max_size: 队列容量上限。
        cooldown_ms: 两次发声的最小间隔。
        ttl_ms: 消息最长存活时间。
        prefix: 键前缀。
    """

    def __init__(
        self,
        client: Redis,
        session_id: str,
        max_size: int = DEFAULT_MAX_SIZE,
        cooldown_ms: int = DEFAULT_COOLDOWN_MS,
        ttl_ms: int = DEFAULT_TTL_MS,
        prefix: str = "sp:session:",
    ) -> None:
        super().__init__(max_size, cooldown_ms, ttl_ms)
        self._client = client
        self._prefix = f"{prefix}{session_id}"
        self._turns_key = f"{self._prefix}:queue"
        self._ids_key = f"{self._prefix}:queue:ids"
        self._cooldown_key = f"{self._prefix}:queue:cooldown"
        self._stats_key = f"{self._prefix}:queue:stats"

    def _bump(self, name: str, amount: int = 1) -> None:
        """计数写入 Redis Hash，重启后仍可观测。"""
        self._client.hincrby(self._stats_key, name, amount)

    def enqueue(self, turn: QueuedTurn) -> EnqueueResult:
        """入队；同一 item_id 只保留一次，超容量淘汰最差项。"""
        added = int(cast(Any, self._client.sadd(self._ids_key, turn.item_id)))
        if not added:
            return EnqueueResult.DUPLICATE
        self._client.zadd(self._turns_key, {turn.dumps(): turn.score})
        self._bump("enqueued")
        result = EnqueueResult.ACCEPTED
        while int(cast(Any, self._client.zcard(self._turns_key))) > (
            self.max_size
        ):
            popped = cast(
                list[tuple[str | bytes, float]],
                self._client.zpopmax(self._turns_key, 1),
            )
            if not popped:
                break
            raw = popped[0][0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            worst = QueuedTurn.loads(raw)
            self._client.srem(self._ids_key, worst.item_id)
            if worst.item_id == turn.item_id:
                result = EnqueueResult.REJECTED_FULL
                continue
            self._bump("evicted")
        return result

    def dequeue(self, now_ms: int) -> QueuedTurn | None:
        """按优先级出队，跳过已过期项，并进入冷却。"""
        deadline = self._client.get(self._cooldown_key)
        if deadline is not None and int(cast(Any, deadline)) > now_ms:
            return None
        while True:
            popped = cast(
                list[tuple[str | bytes, float]],
                self._client.zpopmin(self._turns_key, 1),
            )
            if not popped:
                return None
            raw = popped[0][0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            turn = QueuedTurn.loads(raw)
            self._client.srem(self._ids_key, turn.item_id)
            if turn.expires_at_ms <= now_ms:
                self._bump("dropped_expired")
                continue
            self._client.psetex(
                self._cooldown_key,
                max(self.cooldown_ms, 1),
                str(now_ms + self.cooldown_ms),
            )
            self._bump("dequeued")
            return turn

    def stats(self) -> QueueStats:
        """返回队列指标快照，长度与计数均取自 Redis。"""
        raw = cast(
            dict[Any, Any],
            self._client.hgetall(self._stats_key) or {},
        )
        counters = {
            (key.decode() if isinstance(key, bytes) else str(key)): value
            for key, value in raw.items()
        }
        snapshot = QueueStats()
        for name in ("enqueued", "dequeued", "dropped_expired", "evicted"):
            setattr(snapshot, name, int(counters.get(name, 0)))
        snapshot.size = int(cast(Any, self._client.zcard(self._turns_key)))
        return snapshot
