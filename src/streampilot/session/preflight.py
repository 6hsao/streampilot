"""开播前合规自检。

对应 API.md 中 ``POST /v1/streams/{id}/start`` 的前置校验；
任何一项不通过都禁止开播，并回传失败原因码与说明。
"""

from dataclasses import dataclass
from datetime import UTC, datetime

# 自检项顺序与 API 契约一致，便于前端按序展示
CHECK_ORDER = (
    "platform_auth",
    "avatar_license",
    "voice_license",
    "aigc_label",
    "safety_health",
    "rtmp_credential",
    "rtmp_reachable",
    "push_permit",
    "live2d_model",
    "cubism_license",
)


@dataclass(frozen=True)
class PreflightFacts:
    """自检所需事实，由各子系统查询后汇总。

    Attributes:
        platform_authorized_until: 平台授权到期时间。
        avatar_license_until: 形象授权到期时间。
        voice_license_until: 音色授权到期时间。
        aigc_label_enabled: AIGC 标识是否启用。
        safety_healthy: 内容安全服务是否健康。
        rtmp_credential_ready: 推流凭据是否就绪。
        rtmp_reachable: 推流连通性是否通过。
        push_permit_until: 云端推流许可到期时间。
        live2d_model_ready: Live2D 模型与动作集是否可用。
        cubism_license_recorded: Cubism 商用许可判定是否归档。
    """

    platform_authorized_until: datetime | None = None
    avatar_license_until: datetime | None = None
    voice_license_until: datetime | None = None
    aigc_label_enabled: bool = False
    safety_healthy: bool = False
    rtmp_credential_ready: bool = False
    rtmp_reachable: bool = False
    push_permit_until: datetime | None = None
    live2d_model_ready: bool = False
    cubism_license_recorded: bool = False


@dataclass(frozen=True)
class CheckResult:
    """单项自检结果。

    Attributes:
        code: 自检项标识。
        ok: 是否通过。
        detail: 不通过时的原因。
    """

    code: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PreflightReport:
    """自检报告。

    Attributes:
        results: 全部自检项结果。
    """

    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """是否全部通过。"""
        return all(item.ok for item in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """未通过项。"""
        return tuple(item for item in self.results if not item.ok)

    def as_mapping(self) -> dict[str, bool]:
        """转为 ``开播`` 接口需要的布尔映射。"""
        return {item.code: item.ok for item in self.results}


def _licence_ok(expires: datetime | None, now: datetime) -> tuple[bool, str]:
    """判断授权是否在有效期内。"""
    if expires is None:
        return False, "missing licence expiry"
    expiry = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    if expiry <= now:
        return False, f"licence expired at {expiry.isoformat()}"
    return True, ""


def run_preflight(
    facts: PreflightFacts, *, now: datetime | None = None
) -> PreflightReport:
    """执行开播前自检。

    Args:
        facts: 各子系统汇总的事实。
        now: 当前时间，缺省取 UTC 当前时间。

    Returns:
        按固定顺序排列的自检报告。
    """
    moment = now or datetime.now(UTC)
    checks: dict[str, tuple[bool, str]] = {}

    checks["platform_auth"] = _licence_ok(
        facts.platform_authorized_until, moment
    )
    checks["avatar_license"] = _licence_ok(facts.avatar_license_until, moment)
    checks["voice_license"] = _licence_ok(facts.voice_license_until, moment)
    checks["aigc_label"] = (
        facts.aigc_label_enabled,
        "" if facts.aigc_label_enabled else "AIGC label is disabled",
    )
    checks["safety_health"] = (
        facts.safety_healthy,
        "" if facts.safety_healthy else "content safety is unhealthy",
    )
    checks["rtmp_credential"] = (
        facts.rtmp_credential_ready,
        "" if facts.rtmp_credential_ready else "rtmp credential is missing",
    )
    checks["rtmp_reachable"] = (
        facts.rtmp_reachable,
        "" if facts.rtmp_reachable else "rtmp endpoint is unreachable",
    )
    checks["push_permit"] = _licence_ok(facts.push_permit_until, moment)
    checks["live2d_model"] = (
        facts.live2d_model_ready,
        "" if facts.live2d_model_ready else "live2d model is not ready",
    )
    checks["cubism_license"] = (
        facts.cubism_license_recorded,
        "" if facts.cubism_license_recorded else "cubism licence not recorded",
    )
    return PreflightReport(
        results=tuple(
            CheckResult(code=code, ok=ok, detail=detail)
            for code in CHECK_ORDER
            for ok, detail in (checks[code],)
        )
    )
