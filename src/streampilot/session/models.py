"""场次生命周期：创建、开播自检、收播与异常收播。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from streampilot.persona.models import PersonaSnapshot


def _moment(now: datetime | None) -> datetime:
    """返回注入时间或 UTC 当前时间。"""
    return now or datetime.now(UTC)


class SessionStatus(StrEnum):
    """场次状态。"""

    CREATED = "created"
    LIVE = "live"
    STOPPED = "stopped"


class StopReason(StrEnum):
    """收播原因码。"""

    OPERATOR = "operator"
    COMPLIANCE = "compliance"
    UPSTREAM = "upstream"
    TIMEOUT = "timeout"


class SessionStateError(Exception):
    """非法场次操作。"""


@dataclass(frozen=True)
class SessionConfig:
    """开播时固化的场次配置。

    Attributes:
        persona: 人设快照。
        voice_id: 音色标识。
        avatar_id: 形象标识。
        title: 场次标题。
        platform_account_id: 平台账号标识。
        room_ref: 直播间引用。
    """

    persona: PersonaSnapshot
    voice_id: str
    avatar_id: str
    title: str = ""
    platform_account_id: str = ""
    room_ref: str = ""

    def __post_init__(self) -> None:
        if not self.voice_id.strip():
            raise ValueError("voice_id must not be empty")
        if not self.avatar_id.strip():
            raise ValueError("avatar_id must not be empty")


@dataclass
class StreamSession:
    """一场直播。

    Args:
        session_id: 场次标识。
        config: 场次配置。

    Raises:
        ValueError: 场次标识为空。
    """

    session_id: str
    config: SessionConfig
    status: SessionStatus = SessionStatus.CREATED
    stop_reason: StopReason | None = None
    stop_detail: str = ""
    audit: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")

    def start(
        self,
        checks: Mapping[str, bool],
        operator: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """开播，必须在自检全部通过后调用。

        Args:
            checks: 自检项结果。
            operator: 操作人。
            now: 开播时间，缺省取 UTC 当前时间。

        Raises:
            SessionStateError: 状态非法或自检未通过。
        """
        if self.status is not SessionStatus.CREATED:
            raise SessionStateError(f"cannot start from {self.status}")
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise SessionStateError(
                "preflight failed: " + ", ".join(sorted(failed))
            )
        self.status = SessionStatus.LIVE
        moment = _moment(now)
        if self.started_at is None:
            self.started_at = moment
        self.ended_at = None
        self._record(f"started by {operator}")

    def stop(
        self,
        reason: StopReason,
        detail: str,
        operator: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """收播，记录原因码与说明。

        Args:
            reason: 收播原因码。
            detail: 具体说明。
            operator: 触发人，系统触发时填 ``system``。
            now: 收播时间，缺省取 UTC 当前时间。

        Raises:
            SessionStateError: 非直播中状态。
        """
        if self.status is not SessionStatus.LIVE:
            raise SessionStateError(f"cannot stop from {self.status}")
        self.status = SessionStatus.STOPPED
        self.stop_reason = reason
        self.stop_detail = detail
        self.ended_at = _moment(now)
        self._record(f"stopped by {operator}: {reason.value} {detail}")

    def resume(self, operator: str, *, now: datetime | None = None) -> None:
        """恢复开播：复用原场次配置与新推流凭据。

        Args:
            operator: 操作人。
            now: 恢复时间，缺省取 UTC 当前时间。

        Raises:
            SessionStateError: 状态不是已收播。
        """
        if self.status is not SessionStatus.STOPPED:
            raise SessionStateError(f"cannot resume from {self.status}")
        self.status = SessionStatus.LIVE
        self.stop_reason = None
        self.stop_detail = ""
        self.ended_at = None
        if self.started_at is None:
            self.started_at = _moment(now)
        self._record(f"resumed by {operator}")

    def _record(self, entry: str) -> None:
        self.audit.append(entry)
