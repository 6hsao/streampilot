"""云端推流书面许可登记（GOV-05）。

平台书面许可由运营/合规取得并归档后，把编号、有效期、出口 IP
与归档引用写入部署配置；开播自检引用该记录，缺失或过期即禁止开播。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime


def _utc(value: datetime) -> datetime:
    """把无时区时间按 UTC 解释。"""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class PushPermit:
    """一份已归档的云端推流许可。

    Attributes:
        number: 许可/报备编号，开播自检与审计引用。
        expires_at: 许可到期时间。
        egress_ips: 登记的固定出口 IP。
        archive_ref: 归档引用（工单号、合同号或文档路径）。
        issued_at: 许可生效时间，可省略。
        platform_account_id: 绑定的平台账号；空串表示覆盖全部账号。
        push_params: 登记的推流参数摘要。
        restrictions: 平台附加限制说明。
    """

    number: str
    expires_at: datetime
    egress_ips: tuple[str, ...]
    archive_ref: str
    issued_at: datetime | None = None
    platform_account_id: str = ""
    push_params: str = ""
    restrictions: str = ""

    def __post_init__(self) -> None:
        if not self.number.strip():
            raise ValueError("permit number must not be empty")
        if not self.egress_ips:
            raise ValueError("permit must declare egress ips")
        if not self.archive_ref.strip():
            raise ValueError("permit archive_ref must not be empty")
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.issued_at is not None:
            object.__setattr__(self, "issued_at", _utc(self.issued_at))


@dataclass
class PermitRegistry:
    """进程内许可登记表，按编号去重。"""

    _permits: dict[str, PushPermit] = field(default_factory=dict)

    def register(self, permit: PushPermit) -> None:
        """登记许可；同一编号后写覆盖先写。"""
        self._permits[permit.number] = permit

    def find(self, platform_account_id: str) -> PushPermit | None:
        """按账号匹配许可，账号专属优先于全局许可。

        Args:
            platform_account_id: 场次绑定的平台账号。

        Returns:
            覆盖该账号的许可；没有匹配时返回 None。
        """
        account = platform_account_id.strip()
        scoped = [
            permit
            for permit in self._permits.values()
            if permit.platform_account_id
            and permit.platform_account_id == account
        ]
        candidates = scoped or [
            permit
            for permit in self._permits.values()
            if not permit.platform_account_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.expires_at)

    def __len__(self) -> int:
        """已登记许可数量。"""
        return len(self._permits)
