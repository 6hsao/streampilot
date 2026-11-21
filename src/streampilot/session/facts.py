"""开播自检事实源（LIV-07）。

事实来自部署配置与轻量探测；接入许可台账、审核健康与推流系统后，
可替换为真实子系统的查询实现。
"""

import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Protocol, cast
from urllib.parse import urlsplit

from streampilot.session.models import SessionConfig
from streampilot.session.permit import PermitRegistry, PushPermit
from streampilot.session.preflight import PreflightFacts, PreflightMode

_TRUE = {"1", "true", "yes", "on"}


def probe_rtmp(url: str, *, timeout_s: float = 2.0) -> bool:
    """探测 RTMP 端点 TCP 连通性。

    只做 TCP 连接，不发送 RTMP 握手，也不读取推流凭据。

    Args:
        url: 推流地址，如 ``rtmp://push.example.com/live``。
        timeout_s: 连接超时秒数。

    Returns:
        可建立连接时返回 True。
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "rtmps" else 1935)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _always_false() -> bool:
    """默认探测结果：未接入即视为不可用。"""
    return False


def _always_true() -> bool:
    """固定通过的探测。"""
    return True


class FactSource(Protocol):
    """开播自检事实源。"""

    def collect(self, stream_id: str, config: SessionConfig) -> PreflightFacts:
        """汇总指定场次的自检事实。"""
        ...


@dataclass(frozen=True)
class StaticFactSource:
    """按部署配置汇总自检事实。

    默认全部不可用（fail-closed），必须显式配置后才允许开播。

    Attributes:
        mode: 开播形态；本地模式跳过云端许可与 RTMP 校验。
        platform_authorized_until: 平台账号授权到期时间。
        avatar_license_until: 形象授权到期时间。
        voice_license_until: 音色授权到期时间。
        aigc_label_enabled: AIGC 标识是否启用。
        safety_probe: 内容安全健康探测。
        rtmp_credential_probe: 推流凭据是否就绪。
        rtmp_reachable_probe: 推流连通性探测。
        live2d_model_ready: Live2D 模型与动作集是否可用。
        cubism_license_recorded: Cubism 商用许可判定是否归档。
        permits: 云端推流许可登记表。
        local_broadcast_ready: 本地 OBS/直播伴侣是否就绪。
    """

    mode: PreflightMode = PreflightMode.CLOUD
    platform_authorized_until: datetime | None = None
    avatar_license_until: datetime | None = None
    voice_license_until: datetime | None = None
    aigc_label_enabled: bool = False
    safety_probe: Callable[[], bool] = _always_false
    rtmp_credential_probe: Callable[[], bool] = _always_false
    rtmp_reachable_probe: Callable[[], bool] = _always_false
    live2d_model_ready: bool = False
    cubism_license_recorded: bool = False
    permits: PermitRegistry = field(default_factory=PermitRegistry)
    local_broadcast_ready: bool = False

    def collect(self, stream_id: str, config: SessionConfig) -> PreflightFacts:
        """汇总指定场次的自检事实。

        Args:
            stream_id: 场次标识，预留给按场次查询的凭据源。
            config: 场次配置，用于匹配平台账号许可。

        Returns:
            供 ``run_preflight`` 使用的事实。
        """
        permit = self.permits.find(config.platform_account_id)
        return PreflightFacts(
            mode=self.mode,
            platform_authorized_until=self.platform_authorized_until,
            avatar_license_until=self.avatar_license_until,
            voice_license_until=self.voice_license_until,
            aigc_label_enabled=self.aigc_label_enabled,
            safety_healthy=self.safety_probe(),
            rtmp_credential_ready=self.rtmp_credential_probe(),
            rtmp_reachable=self.rtmp_reachable_probe(),
            push_permit_until=permit.expires_at if permit else None,
            push_permit_number=permit.number if permit else "",
            live2d_model_ready=self.live2d_model_ready,
            cubism_license_recorded=self.cubism_license_recorded,
            local_broadcast_ready=self.local_broadcast_ready,
        )


def _parse_time(raw: str | None) -> datetime | None:
    """解析 ISO-8601 时间；空值返回 None。"""
    if not raw or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 time: {raw!r}") from exc


def _parse_bool(raw: str | None) -> bool:
    """解析布尔环境变量。"""
    return bool(raw and raw.strip().lower() in _TRUE)


def _parse_mode(raw: str | None) -> PreflightMode:
    """解析开播形态，缺省为云端直推。"""
    value = (raw or "").strip().lower()
    if not value:
        return PreflightMode.CLOUD
    try:
        return PreflightMode(value)
    except ValueError as exc:
        raise ValueError(f"unknown BROADCAST_MODE: {raw!r}") from exc


def _env_probe(env: Mapping[str, str], key: str) -> Callable[[], bool]:
    """按环境变量构造固定结果探测。"""
    return _always_true if _parse_bool(env.get(key)) else _always_false


def _permit_from_env(env: Mapping[str, str]) -> PushPermit | None:
    """从环境变量读取推流许可；配置不完整时抛错。"""
    number = env.get("PUSH_PERMIT_NUMBER", "").strip()
    expires_raw = env.get("PUSH_PERMIT_EXPIRES_AT", "").strip()
    egress_raw = env.get("PUSH_PERMIT_EGRESS_IPS", "").strip()
    archive = env.get("PUSH_PERMIT_ARCHIVE_REF", "").strip()
    if not any((number, expires_raw, egress_raw, archive)):
        return None
    missing = [
        key
        for key, value in (
            ("PUSH_PERMIT_NUMBER", number),
            ("PUSH_PERMIT_EXPIRES_AT", expires_raw),
            ("PUSH_PERMIT_EGRESS_IPS", egress_raw),
            ("PUSH_PERMIT_ARCHIVE_REF", archive),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "push permit config incomplete: " + ", ".join(missing)
        )
    expires_at = cast(datetime, _parse_time(expires_raw))
    egress_ips = tuple(
        item.strip() for item in egress_raw.split(",") if item.strip()
    )
    return PushPermit(
        number=number,
        expires_at=expires_at,
        egress_ips=egress_ips,
        archive_ref=archive,
        issued_at=_parse_time(env.get("PUSH_PERMIT_ISSUED_AT")),
        platform_account_id=env.get("PUSH_PERMIT_ACCOUNT_ID", "").strip(),
        push_params=env.get("PUSH_PERMIT_PARAMS", "").strip(),
        restrictions=env.get("PUSH_PERMIT_RESTRICTIONS", "").strip(),
    )


def load_fact_source(env: Mapping[str, str] | None = None) -> StaticFactSource:
    """从环境变量构造自检事实源。

    Args:
        env: 环境变量映射，缺省读取进程环境。

    Returns:
        配置完成的事实源；未配置项按不可用处理。

    Raises:
        ValueError: 时间格式非法或许可配置不完整。
    """
    data = os.environ if env is None else env
    permits = PermitRegistry()
    permit = _permit_from_env(data)
    if permit is not None:
        permits.register(permit)
    reachable_raw = data.get("RTMP_REACHABLE")
    rtmp_url = data.get("RTMP_URL", "").strip()
    timeout_raw = data.get("RTMP_PROBE_TIMEOUT_S", "").strip()
    timeout_s = float(timeout_raw) if timeout_raw else 2.0
    rtmp_probe: Callable[[], bool]
    if reachable_raw is not None and reachable_raw.strip():
        rtmp_probe = (
            _always_true if _parse_bool(reachable_raw) else _always_false
        )
    elif rtmp_url:
        rtmp_probe = partial(probe_rtmp, rtmp_url, timeout_s=timeout_s)
    else:
        rtmp_probe = _always_false
    return StaticFactSource(
        mode=_parse_mode(data.get("BROADCAST_MODE")),
        platform_authorized_until=_parse_time(
            data.get("PLATFORM_AUTH_EXPIRES_AT")
        ),
        avatar_license_until=_parse_time(
            data.get("AVATAR_LICENSE_EXPIRES_AT")
        ),
        voice_license_until=_parse_time(data.get("VOICE_LICENSE_EXPIRES_AT")),
        aigc_label_enabled=_parse_bool(data.get("AIGC_LABEL_ENABLED")),
        safety_probe=_env_probe(data, "SAFETY_HEALTHY"),
        rtmp_credential_probe=_env_probe(data, "RTMP_CREDENTIAL_READY"),
        rtmp_reachable_probe=rtmp_probe,
        live2d_model_ready=_parse_bool(data.get("LIVE2D_MODEL_READY")),
        cubism_license_recorded=_parse_bool(
            data.get("CUBISM_LICENSE_RECORDED")
        ),
        permits=permits,
        local_broadcast_ready=_parse_bool(data.get("LOCAL_BROADCAST_READY")),
    )
