"""安全服务编排：分层、预算、重试与 fail-closed 的测试。"""

import pytest

from streampilot.safety import (
    CloudVerdict,
    MinuteQuota,
    ModerationUnavailableError,
    RuleSet,
    SafetyEngine,
    SafetyService,
    Severity,
    WordRule,
)


class FakeClock:
    """可推进的时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeCloud:
    """按脚本返回结果的假云审核。"""

    name = "fake"

    def __init__(self, script: list[object], clock: FakeClock | None = None):
        self._script = list(script)
        self.calls = 0
        self._clock = clock

    def moderate(self, text: str, direction: str) -> CloudVerdict:
        self.calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, ModerationUnavailableError):
            raise outcome
        assert isinstance(outcome, CloudVerdict)
        return outcome


def _engine() -> SafetyEngine:
    """带一条高危词规则的引擎。"""
    return SafetyEngine(
        RuleSet(
            words=(
                WordRule(
                    term="刷单返利", category="fraud", severity=Severity.BLOCK
                ),
                WordRule(
                    term="微信号", category="traffic", severity=Severity.REVIEW
                ),
            )
        )
    )


def test_local_block_skips_cloud() -> None:
    """本地高危命中直接拦截，不消耗云配额。"""
    cloud = FakeCloud([])
    service = SafetyService(_engine(), cloud)

    decision = service.check("找我做刷单返利")

    assert decision.allowed is False
    assert decision.source == "dfa"
    assert cloud.calls == 0


def test_cloud_pass_and_block() -> None:
    """本地未命中时由云审核给出结论。"""
    cloud = FakeCloud(
        [
            CloudVerdict(status="pass", provider="fake"),
            CloudVerdict(status="block", labels=("politics",)),
        ]
    )
    service = SafetyService(_engine(), cloud)

    first = service.check("今天天气不错")
    second = service.check("聊聊别的话题")

    assert (first.allowed, first.source) == (True, "cloud")
    assert second.allowed is False
    assert second.labels == ("politics",)


def test_local_review_does_not_clear_cloud_block() -> None:
    """本地 REVIEW 与云拦截同时存在时仍拦截。"""
    cloud = FakeCloud([CloudVerdict(status="block", labels=("ad",))])
    service = SafetyService(_engine(), cloud)

    decision = service.check("加个微信号")

    assert decision.allowed is False
    assert decision.status == "block"


def test_local_review_without_cloud_is_allowed_but_marked() -> None:
    """未接入云审核时，REVIEW 结论放行但标记为 review。"""
    service = SafetyService(_engine())

    decision = service.check("加个微信号")

    assert decision.allowed is True
    assert decision.status == "review"


def test_timeout_degrades_within_budget() -> None:
    """云审核超时在预算内降级为拦截，不重试。"""
    clock = FakeClock()

    class SlowCloud(FakeCloud):
        def moderate(self, text: str, direction: str) -> CloudVerdict:
            self.calls += 1
            clock.now += 0.3
            raise ModerationUnavailableError("timed out")

    cloud = SlowCloud([])
    service = SafetyService(_engine(), cloud, clock=clock)

    decision = service.check("普通评论")

    assert decision.allowed is False
    assert decision.unavailable is True
    assert cloud.calls == 1
    assert decision.latency_ms == 300


def test_fast_failure_is_retried_once() -> None:
    """快速失败在预算内会重试一次。"""
    clock = FakeClock()

    class FlakyCloud(FakeCloud):
        def moderate(self, text: str, direction: str) -> CloudVerdict:
            self.calls += 1
            clock.now += 0.05
            outcome = self._script.pop(0)
            if isinstance(outcome, ModerationUnavailableError):
                raise outcome
            assert isinstance(outcome, CloudVerdict)
            return outcome

    cloud = FlakyCloud(
        [
            ModerationUnavailableError("bad gateway"),
            CloudVerdict(status="pass", provider="fake"),
        ]
    )
    service = SafetyService(_engine(), cloud, clock=clock)

    decision = service.check("普通评论")

    assert decision.allowed is True
    assert cloud.calls == 2


def test_quota_exhaustion_is_fail_closed() -> None:
    """云配额耗尽时停止发声。"""
    cloud = FakeCloud([CloudVerdict(status="pass")])
    service = SafetyService(
        _engine(), cloud, quota=MinuteQuota(1, lambda: 0.0)
    )

    first = service.check("第一条")
    second = service.check("第二条")

    assert first.allowed is True
    assert second.allowed is False
    assert second.source == "quota"
    assert cloud.calls == 1


def test_fail_open_is_explicit_opt_in() -> None:
    """关闭 fail-closed 时不可用降级为放行，仅用于本地调试。"""
    cloud = FakeCloud([ModerationUnavailableError("down")])
    service = SafetyService(
        _engine(), cloud, fail_closed=False, max_attempts=1
    )

    decision = service.check("普通评论")

    assert decision.allowed is True
    assert decision.unavailable is True


def test_invalid_parameters_rejected() -> None:
    """非法预算与尝试次数直接报错。"""
    with pytest.raises(ValueError):
        SafetyService(_engine(), budget_ms=0)
    with pytest.raises(ValueError):
        SafetyService(_engine(), max_attempts=0)
    with pytest.raises(ValueError):
        MinuteQuota(0)
