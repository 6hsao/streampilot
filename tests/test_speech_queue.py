"""发言队列的优先级、冷却、过期与容量测试。"""

from typing import Any, cast

import pytest
from redis import Redis

from streampilot.orchestrator.queue import (
    EnqueueResult,
    InMemorySpeechQueue,
    Priority,
    QueuedTurn,
    RedisSpeechQueue,
)


def _queue(
    *,
    max_size: int = 50,
    cooldown_ms: int = 0,
    ttl_ms: int = 60_000,
) -> InMemorySpeechQueue:
    """构造进程内队列。"""
    return InMemorySpeechQueue(
        max_size=max_size, cooldown_ms=cooldown_ms, ttl_ms=ttl_ms
    )


def test_high_priority_jumps_ahead() -> None:
    """高优消息插到普通消息之前。"""
    queue = _queue()

    queue.enqueue(queue.make_turn("c_low", "普通", 1_000))
    queue.enqueue(queue.make_turn("c_high", "点名", 1_000, Priority.HIGH))

    first = queue.dequeue(1_000)

    assert first is not None
    assert first.item_id == "c_high"


def test_priority_window_does_not_starve_old_items() -> None:
    """优先级只在本窗口内插队，旧消息不会被永久压制。"""
    queue = _queue(ttl_ms=600_000)

    queue.enqueue(queue.make_turn("c_old", "等了很久", 1_000))
    queue.enqueue(
        queue.make_turn("c_high", "刚来的高优", 61_500, Priority.URGENT)
    )

    first = queue.dequeue(61_500)

    assert first is not None
    assert first.item_id == "c_old"


def test_fifo_within_same_priority() -> None:
    """同优先级按入队顺序发声。"""
    queue = _queue()

    queue.enqueue(queue.make_turn("c_1", "第一条", 1_000))
    queue.enqueue(queue.make_turn("c_2", "第二条", 1_000))

    first = queue.dequeue(1_000)
    second = queue.dequeue(1_000)

    assert first is not None and second is not None
    assert (first.item_id, second.item_id) == ("c_1", "c_2")


def test_duplicate_is_rejected() -> None:
    """同一 item_id 只入队一次。"""
    queue = _queue()

    assert queue.enqueue(queue.make_turn("c_1", "a", 1_000)) is (
        EnqueueResult.ACCEPTED
    )
    assert queue.enqueue(queue.make_turn("c_1", "b", 1_001)) is (
        EnqueueResult.DUPLICATE
    )
    assert queue.stats().size == 1


def test_cooldown_blocks_next_speech() -> None:
    """冷却期内不出队，冷却结束后恢复。"""
    queue = _queue(cooldown_ms=2_000)

    queue.enqueue(queue.make_turn("c_1", "a", 1_000))
    queue.enqueue(queue.make_turn("c_2", "b", 1_000))

    assert queue.dequeue(1_000) is not None
    assert queue.dequeue(1_500) is None
    assert queue.dequeue(3_000) is not None


def test_expired_turns_are_dropped() -> None:
    """过期消息被丢弃且不发声。"""
    queue = _queue(ttl_ms=1_000)

    queue.enqueue(queue.make_turn("c_1", "太旧了", 1_000))

    assert queue.dequeue(2_001) is None
    stats = queue.stats()
    assert stats.size == 0
    assert stats.dropped_expired == 1
    assert stats.dequeued == 0


def test_capacity_evicts_lowest_priority() -> None:
    """满队列时淘汰优先级最低的消息。"""
    queue = _queue(max_size=2)

    queue.enqueue(queue.make_turn("c_normal", "普通", 1_000))
    queue.enqueue(queue.make_turn("c_high", "高优", 1_000, Priority.HIGH))

    result = queue.enqueue(
        queue.make_turn("c_urgent", "紧急", 1_000, Priority.URGENT)
    )

    assert result is EnqueueResult.ACCEPTED
    assert queue.stats().size == 2
    assert queue.stats().evicted == 1
    first = queue.dequeue(1_000)
    second = queue.dequeue(1_000)
    assert first is not None and second is not None
    assert (first.item_id, second.item_id) == ("c_urgent", "c_high")


def test_capacity_rejects_weakest_newcomer() -> None:
    """新消息优先级最低时直接拒绝，不挤掉在队消息。"""
    queue = _queue(max_size=1)

    queue.enqueue(queue.make_turn("c_high", "高优", 1_000, Priority.HIGH))

    result = queue.enqueue(queue.make_turn("c_normal", "普通", 1_000))

    assert result is EnqueueResult.REJECTED_FULL
    assert queue.stats().size == 1


def test_invalid_parameters_are_rejected() -> None:
    """非法容量、冷却与 TTL 直接报错。"""
    with pytest.raises(ValueError):
        InMemorySpeechQueue(max_size=0)
    with pytest.raises(ValueError):
        InMemorySpeechQueue(cooldown_ms=-1)
    with pytest.raises(ValueError):
        InMemorySpeechQueue(ttl_ms=0)


def test_turn_requires_sane_expiry() -> None:
    """过期时间必须晚于入队时间。"""
    with pytest.raises(ValueError):
        QueuedTurn("c_1", "a", 1_000, 1_000)
    with pytest.raises(ValueError):
        QueuedTurn("", "a", 1_000, 2_000)


def test_four_hour_traffic_stays_bounded_and_never_repeats() -> None:
    """4 小时模拟流量：队列不溢出，且同一条消息不会发声两次。"""
    queue = _queue(max_size=20, cooldown_ms=2_000, ttl_ms=30_000)
    spoken: set[str] = set()
    now_ms = 0
    # 每 1.5 秒一条评论，共 4 小时
    for index in range(9_600):
        now_ms += 1_500
        queue.enqueue(queue.make_turn(f"c_{index}", f"评论{index}", now_ms))
        turn = queue.dequeue(now_ms)
        if turn is not None:
            assert turn.item_id not in spoken
            spoken.add(turn.item_id)
        assert queue.stats().size <= 20

    assert spoken
    stats = queue.stats()
    assert stats.dequeued == len(spoken)
    assert stats.size <= 20
    assert stats.enqueued > 9_000


class _FakeRedis:
    """仅实现发言队列用到的最小 Redis 命令，用于验证 Redis 队列逻辑。"""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.sets: dict[str, set[str]] = {}
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, int]] = {}

    def sadd(self, key: str, member: str) -> int:
        target = self.sets.setdefault(key, set())
        if member in target:
            return 0
        target.add(member)
        return 1

    def srem(self, key: str, member: str) -> int:
        target = self.sets.get(key)
        if target is None or member not in target:
            return 0
        target.discard(member)
        return 1

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zpopmax(self, key: str, count: int = 1) -> list[tuple[str, float]]:
        return self._pop(key, count, reverse=True)

    def zpopmin(self, key: str, count: int = 1) -> list[tuple[str, float]]:
        return self._pop(key, count, reverse=False)

    def _pop(
        self, key: str, count: int, *, reverse: bool
    ) -> list[tuple[str, float]]:
        target = self.zsets.get(key, {})
        ordered = sorted(target.items(), key=lambda kv: kv[1], reverse=reverse)
        popped = [(member, score) for member, score in ordered[:count]]
        for member, _ in popped:
            del target[member]
        return popped

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def psetex(self, key: str, milliseconds: int, value: str) -> bool:
        self.values[key] = value
        return True

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    def hgetall(self, key: str) -> dict[str, int]:
        return dict(self.hashes.get(key, {}))


def _redis_queue(
    *,
    max_size: int = 50,
    cooldown_ms: int = 0,
    ttl_ms: int = 60_000,
) -> RedisSpeechQueue:
    """构造挂在假 Redis 上的队列。"""
    client = cast(Redis, cast(Any, _FakeRedis()))
    return RedisSpeechQueue(
        client,
        session_id="ss_test",
        max_size=max_size,
        cooldown_ms=cooldown_ms,
        ttl_ms=ttl_ms,
    )


def test_redis_queue_orders_by_priority_and_dedupes() -> None:
    """Redis 队列同样按优先级出队并去重。"""
    queue = _redis_queue()

    assert queue.enqueue(queue.make_turn("c_low", "普通", 1_000)) is (
        EnqueueResult.ACCEPTED
    )
    assert queue.enqueue(queue.make_turn("c_low", "重复", 1_000)) is (
        EnqueueResult.DUPLICATE
    )
    queue.enqueue(queue.make_turn("c_high", "高优", 1_000, Priority.HIGH))

    first = queue.dequeue(1_000)
    second = queue.dequeue(1_000)

    assert first is not None and second is not None
    assert (first.item_id, second.item_id) == ("c_high", "c_low")
    assert queue.dequeue(1_000) is None
    assert queue.stats().enqueued == 2
    assert queue.stats().dequeued == 2


def test_redis_queue_honours_cooldown_and_expiry() -> None:
    """Redis 队列遵守冷却时间并丢弃过期项。"""
    queue = _redis_queue(cooldown_ms=2_000, ttl_ms=1_000)

    queue.enqueue(queue.make_turn("c_new", "新鲜", 5_000))
    queue.enqueue(queue.make_turn("c_old", "过期", 1_000))

    first = queue.dequeue(5_000)

    assert first is not None
    assert first.item_id == "c_new"
    assert queue.dequeue(6_000) is None
    assert queue.dequeue(8_000) is None
    assert queue.stats().dropped_expired == 1


def test_redis_queue_evicts_weakest_on_capacity() -> None:
    """Redis 队列超容量时淘汰分值最差项。"""
    queue = _redis_queue(max_size=2)

    queue.enqueue(queue.make_turn("c_normal", "普通", 1_000))
    queue.enqueue(queue.make_turn("c_high", "高优", 1_000, Priority.HIGH))
    result = queue.enqueue(
        queue.make_turn("c_urgent", "紧急", 1_000, Priority.URGENT)
    )

    assert result is EnqueueResult.ACCEPTED
    assert queue.stats().size == 2
    assert queue.stats().evicted == 1

    rejected = queue.enqueue(queue.make_turn("c_last", "普通", 1_000))

    assert rejected is EnqueueResult.REJECTED_FULL
    assert queue.stats().size == 2
