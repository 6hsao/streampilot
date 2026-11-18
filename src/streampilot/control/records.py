"""控制面审计记录与接管信息读取。"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from streampilot.contracts import ErrorCode
from streampilot.control.models import AuditEntry, TakeoverInfo
from streampilot.control.session import SessionControl
from streampilot.control.store import AuditStore, TakeoverStore, new_id


class AuditRecorder:
    """写入操作审计，并读取当前接管信息。

    Args:
        audit: 审计存储。
        takeovers: 接管记录存储。
        org_id: 组织标识。
        clock: 时间源。
    """

    def __init__(
        self,
        *,
        audit: AuditStore,
        takeovers: TakeoverStore,
        org_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._audit = audit
        self._takeovers = takeovers
        self._org_id = org_id
        self._clock = clock

    def state_snapshot(self, control: SessionControl) -> dict[str, Any]:
        """构造写入审计的状态快照。"""
        return {
            "state": control.state.state.value,
            "queue_depth": control.queue.stats().size,
            "paused": control.paused,
            "muted": control.muted,
            "takeover": (
                self._takeovers.active(control.stream_id) is not None
            ),
        }

    def takeover_info(self, stream_id: str) -> TakeoverInfo:
        """读取当前接管状态。"""
        record = self._takeovers.active(stream_id)
        if record is None:
            return TakeoverInfo()
        return TakeoverInfo(
            active=True,
            operator_id=record.operator_id,
            reason=record.reason,
            started_at=record.started_at,
        )

    def record(
        self,
        stream_id: str,
        actor_id: str,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        """写入一条审计记录。"""
        self._audit.append(
            AuditEntry(
                id=new_id(),
                org_id=self._org_id,
                actor_id=actor_id,
                action=action,
                target=stream_id,
                before=before,
                after=after,
                created_at=self._clock(),
            )
        )

    def rejected(
        self,
        stream_id: str,
        actor_id: str,
        action: str,
        before: dict[str, Any] | None,
        code: ErrorCode,
        reason: str,
    ) -> None:
        """记录被拒绝的操作，保留可追溯性。"""
        after: dict[str, Any] = {"error": code.value}
        if reason:
            after["reason"] = reason
        self.record(stream_id, actor_id, action, before, after)
