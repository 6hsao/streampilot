"""推流许可与开播自检事实源的测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from streampilot.persona import PersonaSnapshot
from streampilot.session.facts import load_fact_source, probe_rtmp
from streampilot.session.models import SessionConfig
from streampilot.session.permit import PermitRegistry, PushPermit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _config(account: str = "acc_1") -> SessionConfig:
    """构造带平台账号的场次配置。"""
    return SessionConfig(
        persona=PersonaSnapshot(
            persona_id="p_1", version=1, prompt_digest="d"
        ),
        voice_id="voice_1",
        avatar_id="avatar_1",
        platform_account_id=account,
    )


def _permit(**overrides: object) -> PushPermit:
    """构造一份完整许可。"""
    base: dict[str, object] = {
        "number": "permit_001",
        "expires_at": NOW + timedelta(days=30),
        "egress_ips": ("203.0.113.10",),
        "archive_ref": "GOV-05/2026-001",
        "issued_at": NOW - timedelta(days=1),
    }
    base.update(overrides)
    return PushPermit(**base)  # type: ignore[arg-type]


class _Connection:
    """socket.create_connection 的假上下文管理器。"""

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_permit_requires_complete_material() -> None:
    """编号、出口 IP 与归档引用缺一不可。"""
    with pytest.raises(ValueError):
        _permit(number=" ")
    with pytest.raises(ValueError):
        _permit(egress_ips=())
    with pytest.raises(ValueError):
        _permit(archive_ref="")


def test_registry_matches_account_and_latest_expiry() -> None:
    """账号专属许可优先，同范围内取到期最晚的一份。"""
    registry = PermitRegistry()
    global_early = _permit(
        number="permit_global_early",
        expires_at=NOW + timedelta(days=10),
    )
    global_late = _permit(
        number="permit_global_late",
        expires_at=NOW + timedelta(days=40),
    )
    account_permit = _permit(
        number="permit_acc",
        expires_at=NOW + timedelta(days=20),
        platform_account_id="acc_1",
    )
    registry.register(global_early)
    registry.register(global_late)
    registry.register(account_permit)

    assert len(registry) == 3
    assert registry.find("acc_1") is account_permit
    assert registry.find("acc_2") is global_late
    assert registry.find("acc_3") is global_late

    empty = PermitRegistry()
    assert empty.find("acc_1") is None


def test_load_fact_source_defaults_to_fail_closed() -> None:
    """未配置时全部自检事实不可用。"""
    source = load_fact_source({})

    facts = source.collect("st_1", _config())

    assert facts.push_permit_until is None
    assert facts.push_permit_number == ""
    assert facts.aigc_label_enabled is False
    assert facts.safety_healthy is False
    assert facts.rtmp_credential_ready is False
    assert facts.rtmp_reachable is False


def test_load_fact_source_reads_permit_and_flags() -> None:
    """完整配置解析许可、授权时间与开关。"""
    source = load_fact_source(
        {
            "PUSH_PERMIT_NUMBER": "permit_001",
            "PUSH_PERMIT_EXPIRES_AT": "2026-02-01T00:00:00Z",
            "PUSH_PERMIT_EGRESS_IPS": "203.0.113.10, 203.0.113.11",
            "PUSH_PERMIT_ARCHIVE_REF": "GOV-05/2026-001",
            "PUSH_PERMIT_ACCOUNT_ID": "acc_1",
            "PLATFORM_AUTH_EXPIRES_AT": "2026-02-01T00:00:00Z",
            "AVATAR_LICENSE_EXPIRES_AT": "2026-02-01T00:00:00Z",
            "VOICE_LICENSE_EXPIRES_AT": "2026-02-01T00:00:00Z",
            "AIGC_LABEL_ENABLED": "true",
            "SAFETY_HEALTHY": "1",
            "RTMP_CREDENTIAL_READY": "yes",
            "RTMP_REACHABLE": "on",
            "LIVE2D_MODEL_READY": "true",
            "CUBISM_LICENSE_RECORDED": "TRUE",
        }
    )

    facts = source.collect("st_1", _config())

    assert facts.push_permit_number == "permit_001"
    assert facts.push_permit_until == datetime(2026, 2, 1, tzinfo=UTC)
    assert facts.aigc_label_enabled is True
    assert facts.safety_healthy is True
    assert facts.rtmp_credential_ready is True
    assert facts.rtmp_reachable is True
    assert facts.live2d_model_ready is True
    assert facts.cubism_license_recorded is True
    permit = source.permits.find("acc_1")
    assert permit is not None
    assert permit.egress_ips == ("203.0.113.10", "203.0.113.11")


def test_load_fact_source_rejects_incomplete_permit() -> None:
    """许可配置缺项时启动即失败，避免静默放行。"""
    with pytest.raises(ValueError, match="incomplete"):
        load_fact_source({"PUSH_PERMIT_NUMBER": "permit_001"})


def test_load_fact_source_rejects_bad_time() -> None:
    """非法时间格式直接报错。"""
    env = {
        "PUSH_PERMIT_NUMBER": "permit_001",
        "PUSH_PERMIT_EXPIRES_AT": "not-a-time",
        "PUSH_PERMIT_EGRESS_IPS": "203.0.113.10",
        "PUSH_PERMIT_ARCHIVE_REF": "GOV-05/2026-001",
    }

    with pytest.raises(ValueError, match="ISO-8601"):
        load_fact_source(env)


def test_load_fact_source_probes_rtmp_url() -> None:
    """未设置结果开关时按推流地址探测。"""
    source = load_fact_source({"RTMP_URL": "not-a-url"})

    facts = source.collect("st_1", _config())

    assert facts.rtmp_reachable is False


def test_probe_rtmp_handles_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测成功、连接失败与非法地址都返回明确结果。"""
    monkeypatch.setattr(
        "streampilot.session.facts.socket.create_connection",
        lambda *args, **kwargs: _Connection(),
    )
    assert probe_rtmp("rtmp://push.example.com/live") is True
    assert probe_rtmp("rtmps://push.example.com/live") is True
    assert probe_rtmp("not-a-url") is False

    def _refused(*args: object, **kwargs: object) -> _Connection:
        raise OSError("connection refused")

    monkeypatch.setattr(
        "streampilot.session.facts.socket.create_connection", _refused
    )
    assert probe_rtmp("rtmp://push.example.com/live") is False
