"""场次管理接口的契约测试。"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from streampilot.control.app import create_app
from streampilot.control.service import ControlService
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.session.app import create_session_router
from streampilot.session.facts import StaticFactSource
from streampilot.session.manager import SessionManager
from streampilot.session.permit import PermitRegistry, PushPermit

NOW = datetime(2026, 1, 1, tzinfo=UTC)
HEADERS = {"X-Operator-Id": "op_1"}
CREATE_BODY = {
    "persona_id": "p_1",
    "persona_version": 1,
    "prompt_digest": "digest_abc",
    "voice_id": "voice_1",
    "avatar_id": "avatar_1",
    "title": "接口测试场次",
}


def _passing_source() -> StaticFactSource:
    """构造全部通过的事实源。"""
    permits = PermitRegistry()
    permits.register(
        PushPermit(
            number="permit_001",
            expires_at=NOW + timedelta(days=30),
            egress_ips=("203.0.113.10",),
            archive_ref="GOV-05/2026-001",
        )
    )
    return StaticFactSource(
        platform_authorized_until=NOW + timedelta(days=30),
        avatar_license_until=NOW + timedelta(days=30),
        voice_license_until=NOW + timedelta(days=30),
        aigc_label_enabled=True,
        safety_probe=lambda: True,
        rtmp_credential_probe=lambda: True,
        rtmp_reachable_probe=lambda: True,
        live2d_model_ready=True,
        cubism_license_recorded=True,
        permits=permits,
    )


def _client(facts: StaticFactSource | None = None) -> TestClient:
    """构建带场次管理路由的测试客户端。"""
    audit = InMemoryAuditStore()
    control = ControlService(
        audit=audit,
        takeovers=InMemoryTakeoverStore(),
        clock=lambda: NOW,
    )
    manager = SessionManager(
        control=control,
        audit=audit,
        clock=lambda: NOW,
        facts=facts or _passing_source(),
    )
    app = create_app(control)
    app.include_router(create_session_router(manager))
    return TestClient(app)


def _create(client: TestClient) -> str:
    """创建场次并返回流标识。"""
    response = client.post("/v1/streams", json=CREATE_BODY, headers=HEADERS)
    assert response.status_code == 201
    stream_id = response.json()["stream_id"]
    assert isinstance(stream_id, str)
    return stream_id


def test_lifecycle_over_http() -> None:
    """创建、开播、收播、恢复的完整流程。"""
    client = _client()
    stream_id = _create(client)

    started = client.post(
        f"/v1/streams/{stream_id}/start",
        json={},
        headers=HEADERS,
    )
    assert started.status_code == 200
    assert started.json()["status"] == "live"
    assert started.json()["state"] == "idle"

    stopped = client.post(
        f"/v1/streams/{stream_id}/stop",
        json={"reason": "upstream", "detail": "rtmp disconnected"},
        headers=HEADERS,
    )
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["status"] == "stopped"
    assert body["state"] == "ended"
    assert body["stop_reason"] == "upstream"

    state = client.get(f"/v1/streams/{stream_id}/state", headers=HEADERS)
    assert state.json()["state"] == "ended"

    resumed = client.post(
        f"/v1/streams/{stream_id}/resume",
        json={"reason": "推流地址已更新"},
        headers=HEADERS,
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "live"
    assert resumed.json()["state"] == "idle"
    assert resumed.json()["stop_reason"] is None


def test_create_returns_config_snapshot() -> None:
    """创建响应包含固化的配置快照。"""
    client = _client()

    response = client.post("/v1/streams", json=CREATE_BODY, headers=HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["persona_id"] == "p_1"
    assert body["persona_version"] == 1
    assert body["prompt_digest"] == "digest_abc"
    assert body["voice_id"] == "voice_1"
    assert body["avatar_id"] == "avatar_1"
    assert body["created_at"].startswith("2026-01-01T00:00:00")


def test_start_blocked_when_preflight_fails() -> None:
    """自检未通过返回 403 与失败项清单。"""
    client = _client(facts=StaticFactSource())
    stream_id = _create(client)

    response = client.post(
        f"/v1/streams/{stream_id}/start",
        json={},
        headers=HEADERS,
    )

    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "PERMISSION_DENIED"
    assert "push_permit" in error["details"]["failed_checks"]


def test_duplicate_start_returns_409() -> None:
    """重复开播返回状态冲突。"""
    client = _client()
    stream_id = _create(client)
    client.post(f"/v1/streams/{stream_id}/start", json={}, headers=HEADERS)

    response = client.post(
        f"/v1/streams/{stream_id}/start", json={}, headers=HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATE_CONFLICT"


def test_invalid_stop_reason_returns_400() -> None:
    """未知收播原因码返回参数错误。"""
    client = _client()
    stream_id = _create(client)

    response = client.post(
        f"/v1/streams/{stream_id}/stop",
        json={"reason": "explode"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_missing_operator_returns_401() -> None:
    """创建场次同样要求操作人请求头。"""
    client = _client()

    response = client.post("/v1/streams", json=CREATE_BODY)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_unknown_stream_returns_404() -> None:
    """未知场次开播返回 404。"""
    client = _client()

    response = client.post(
        "/v1/streams/st_missing/start", json={}, headers=HEADERS
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
