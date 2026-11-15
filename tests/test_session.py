"""开播自检与场次生命周期的测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from streampilot.persona import PersonaSnapshot
from streampilot.session import (
    CHECK_ORDER,
    PreflightFacts,
    SessionConfig,
    SessionStateError,
    SessionStatus,
    StopReason,
    StreamSession,
    run_preflight,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _facts(**overrides: object) -> PreflightFacts:
    """构造全部通过的自检事实。"""
    base: dict[str, object] = {
        "platform_authorized_until": NOW + timedelta(days=30),
        "avatar_license_until": NOW + timedelta(days=30),
        "voice_license_until": NOW + timedelta(days=30),
        "aigc_label_enabled": True,
        "safety_healthy": True,
        "rtmp_credential_ready": True,
        "rtmp_reachable": True,
        "push_permit_until": NOW + timedelta(days=30),
        "live2d_model_ready": True,
        "cubism_license_recorded": True,
    }
    base.update(overrides)
    return PreflightFacts(**base)  # type: ignore[arg-type]


def _config() -> SessionConfig:
    """构造场次配置。"""
    return SessionConfig(
        persona=PersonaSnapshot(
            persona_id="p_1", version=1, prompt_digest="abc"
        ),
        voice_id="voice_001",
        avatar_id="avatar_001",
    )


def test_preflight_passes_when_everything_is_ready() -> None:
    """全部就绪时自检通过，结果按固定顺序排列。"""
    report = run_preflight(_facts(), now=NOW)

    assert report.ok is True
    assert [item.code for item in report.results] == list(CHECK_ORDER)


def test_preflight_flags_expired_licence() -> None:
    """授权过期即禁止开播，并给出原因。"""
    report = run_preflight(
        _facts(avatar_license_until=NOW - timedelta(minutes=1)), now=NOW
    )

    assert report.ok is False
    failures = {item.code: item.detail for item in report.failures}
    assert "avatar_license" in failures
    assert "expired" in failures["avatar_license"]


def test_preflight_requires_push_permit_and_cubism_record() -> None:
    """推流许可与 Cubism 判定缺失都阻断开播。"""
    report = run_preflight(
        _facts(push_permit_until=None, cubism_license_recorded=False),
        now=NOW,
    )

    codes = {item.code for item in report.failures}

    assert codes == {"push_permit", "cubism_license"}


def test_preflight_treats_naive_datetime_as_utc() -> None:
    """无时区的到期时间按 UTC 解释。"""
    report = run_preflight(
        _facts(platform_authorized_until=datetime(2026, 1, 2, 12, 0)),
        now=NOW,
    )

    assert report.ok is True


def test_as_mapping_matches_codes() -> None:
    """布尔映射可直接传给场次 start。"""
    mapping = run_preflight(_facts(), now=NOW).as_mapping()

    assert mapping == {code: True for code in CHECK_ORDER}


def test_session_start_requires_all_checks() -> None:
    """自检未通过时禁止开播且状态不变。"""
    session = StreamSession("ss_1", _config())

    with pytest.raises(SessionStateError, match="preflight failed"):
        session.start({"platform_auth": True, "safety_health": False}, "op")

    assert session.status is SessionStatus.CREATED


def _stop_reason(session: StreamSession) -> StopReason | None:
    """读取收播原因，避免静态类型收窄影响后续断言。"""
    return session.stop_reason


def test_session_lifecycle_and_audit() -> None:
    """开播、异常收播、恢复都有审计记录。"""
    session = StreamSession("ss_1", _config())
    checks = run_preflight(_facts(), now=NOW).as_mapping()

    session.start(checks, "op-1")
    assert session.status is SessionStatus.LIVE

    session.stop(StopReason.UPSTREAM, "rtmp disconnected", "system")
    assert session.status.value == "stopped"
    assert session.stop_reason is StopReason.UPSTREAM

    session.resume("op-2")
    audit = session.audit
    assert session.status.value == "live"
    assert _stop_reason(session) is None
    assert len(audit) == 3


def test_illegal_transitions_are_rejected() -> None:
    """重复开播、未开播就收播、直播中恢复都非法。"""
    session = StreamSession("ss_1", _config())

    with pytest.raises(SessionStateError):
        session.stop(StopReason.OPERATOR, "x", "op")

    session.start({"a": True}, "op")
    with pytest.raises(SessionStateError):
        session.start({"a": True}, "op")
    with pytest.raises(SessionStateError):
        session.resume("op")


def test_config_rejects_empty_identifiers() -> None:
    """音色与形象标识不能为空。"""
    persona = PersonaSnapshot(persona_id="p_1", version=1, prompt_digest="d")

    with pytest.raises(ValueError):
        SessionConfig(persona=persona, voice_id=" ", avatar_id="a")
    with pytest.raises(ValueError):
        SessionConfig(persona=persona, voice_id="v", avatar_id=" ")


def test_session_requires_identifier() -> None:
    """场次标识不能为空。"""
    with pytest.raises(ValueError):
        StreamSession("  ", _config())
