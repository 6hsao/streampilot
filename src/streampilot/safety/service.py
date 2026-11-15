"""内容安全服务：DFA 前置、云审核兜底、预算与配额控制。

处置原则：任一环节「拦截」或「不可用」都不发声（fail-closed）；
规则先挡，能省一次云调用；云审核超时不重试第二次，确保 300ms 内降级。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from streampilot.safety.cloud import (
    CloudModerator,
    Direction,
    MinuteQuota,
    ModerationUnavailableError,
    Status,
)
from streampilot.safety.dfa import SafetyEngine
from streampilot.safety.models import Action

# 云审核超时预算，与 ARCHITECTURE 的 300ms 一致
DEFAULT_BUDGET_MS = 300
# 默认最多两次尝试；时间预算不足时不再重试
DEFAULT_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class SafetyDecision:
    """一次审核的最终结论。

    Attributes:
        allowed: 是否允许发声。
        status: 归一化结论。
        source: 结论来源，``dfa`` / ``cloud`` / ``quota`` / ``cloud_error``。
        labels: 命中标签。
        reason: 人类可读原因，写入审核留痕。
        latency_ms: 审核总耗时。
        unavailable: 是否为审核不可用导致的拦截。
    """

    allowed: bool
    status: Status
    source: str
    labels: tuple[str, ...] = ()
    reason: str = ""
    latency_ms: int = 0
    unavailable: bool = False


class SafetyService:
    """组合规则引擎与云审核的审核服务。

    Args:
        engine: 本地 DFA 规则引擎。
        cloud: 云审核适配器；缺省时只跑本地规则。
        budget_ms: 单次审核的时间预算。
        max_attempts: 云审核最多尝试次数。
        quota: 云审核分钟配额。
        fail_closed: 云审核不可用时是否拦截，生产必须为 True。
        clock: 时间源，便于测试注入。
    """

    def __init__(
        self,
        engine: SafetyEngine,
        cloud: CloudModerator | None = None,
        *,
        budget_ms: int = DEFAULT_BUDGET_MS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        quota: MinuteQuota | None = None,
        fail_closed: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._engine = engine
        self._cloud = cloud
        self._budget_ms = budget_ms
        self._max_attempts = max_attempts
        self._quota = quota
        self._fail_closed = fail_closed
        self._clock = clock

    def check(
        self, text: str, direction: Direction = "input"
    ) -> SafetyDecision:
        """审核一段文本。

        Args:
            text: 待审核文本。
            direction: 输入或输出。

        Returns:
            审核结论；永不抛出业务异常。
        """
        started = self._clock()
        local = self._engine.scan(text)
        if local.action is Action.BLOCK:
            return SafetyDecision(
                allowed=False,
                status="block",
                source="dfa",
                labels=tuple(
                    f"{hit.category}:{hit.term}" for hit in local.hits
                ),
                reason="blocked by local lexicon",
                latency_ms=self._elapsed_ms(started),
            )
        if self._cloud is None:
            return SafetyDecision(
                allowed=True,
                status="review" if local.action is Action.REVIEW else "pass",
                source="dfa",
                labels=tuple(
                    f"{hit.category}:{hit.term}" for hit in local.hits
                ),
                reason="local rules only",
                latency_ms=self._elapsed_ms(started),
            )
        return self._check_cloud(
            text,
            direction,
            started,
            local_review=local.action is Action.REVIEW,
        )

    def _check_cloud(
        self,
        text: str,
        direction: Direction,
        started: float,
        *,
        local_review: bool,
    ) -> SafetyDecision:
        """在预算内调用云审核，失败按 fail-closed 处理。"""
        for attempt in range(1, self._max_attempts + 1):
            if self._quota is not None and not self._quota.try_acquire():
                return self._unavailable(
                    started, "cloud moderation quota exhausted", "quota"
                )
            try:
                assert self._cloud is not None
                verdict = self._cloud.moderate(text, direction)
            except ModerationUnavailableError as exc:
                if not self._can_retry(started, attempt):
                    return self._unavailable(started, str(exc), "cloud_error")
                continue
            if verdict.status == "block":
                status: Status = "block"
            elif local_review:
                status = "review"
            else:
                status = verdict.status
            return SafetyDecision(
                allowed=verdict.status != "block",
                status=status,
                source="cloud",
                labels=verdict.labels,
                reason="cloud moderation",
                latency_ms=self._elapsed_ms(started),
            )
        return self._unavailable(
            started, "cloud moderation unavailable", "cloud_error"
        )

    def _can_retry(self, started: float, attempt: int) -> bool:
        """仅在预算仍有剩余且未达上限时重试。"""
        if attempt >= self._max_attempts:
            return False
        return self._elapsed_ms(started) < self._budget_ms

    def _unavailable(
        self,
        started: float,
        reason: str,
        source: str,
    ) -> SafetyDecision:
        """构造不可用结论，按配置决定是否拦截。"""
        return SafetyDecision(
            allowed=not self._fail_closed,
            status="block" if self._fail_closed else "pass",
            source=source,
            reason=reason,
            latency_ms=self._elapsed_ms(started),
            unavailable=True,
        )

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)
