"""控制面接口的契约测试。"""

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from streampilot.control.app import CONSOLE_PATH, create_app
from streampilot.control.service import ControlService
from streampilot.control.session import SessionControl
from streampilot.control.sink import LoggingControlSink
from streampilot.control.store import (
    InMemoryAuditStore,
    InMemoryTakeoverStore,
)
from streampilot.orchestrator.queue import InMemorySpeechQueue
from streampilot.orchestrator.state import SessionStateMachine

HEADERS = {"X-Operator-Id": "op_1"}


def _client() -> TestClient:
    """构建带内存场次的测试客户端。"""
    service = ControlService(
        audit=InMemoryAuditStore(),
        takeovers=InMemoryTakeoverStore(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    service.register(
        SessionControl(
            stream_id="st_1",
            state=SessionStateMachine(),
            queue=InMemorySpeechQueue(cooldown_ms=0),
            sink=LoggingControlSink(),
            templates={"welcome": "欢迎来到直播间"},
        )
    )
    return TestClient(create_app(service))


def test_takeover_flow_over_http() -> None:
    """接管、查询状态与释放的完整接口流程。"""
    client = _client()

    taken = client.post(
        "/v1/streams/st_1/takeover",
        json={"action": "take", "reason": "敏感话题升级处理"},
        headers=HEADERS,
    )
    assert taken.status_code == 200
    body = taken.json()
    assert body["state"] == "takeover"
    assert body["takeover"]["active"] is True
    assert body["ai_silent"] is True

    state = client.get("/v1/streams/st_1/state", headers=HEADERS)
    assert state.status_code == 200
    snapshot = state.json()
    assert snapshot["state"] == "takeover"
    assert snapshot["ai_silent"] is True
    assert snapshot["templates"] == ["welcome"]

    audit = client.get("/v1/streams/st_1/audit", headers=HEADERS)
    assert audit.status_code == 200
    entries = audit.json()["entries"]
    assert [item["action"] for item in entries] == ["takeover.take"]
    assert entries[0]["actor_id"] == "op_1"

    released = client.post(
        "/v1/streams/st_1/takeover",
        json={"action": "release", "reason": "已核实并处理"},
        headers=HEADERS,
    )
    assert released.status_code == 200
    assert released.json()["state"] == "idle"
    assert released.json()["takeover"]["conclusion"] == "已核实并处理"


def test_control_action_over_http() -> None:
    """暂停与恢复经 HTTP 生效。"""
    client = _client()

    paused = client.post(
        "/v1/streams/st_1/control",
        json={"action": "pause", "reason": "operator action"},
        headers=HEADERS,
    )
    assert paused.status_code == 200
    assert paused.json()["ai_silent"] is True

    resumed = client.post(
        "/v1/streams/st_1/control",
        json={"action": "resume"},
        headers=HEADERS,
    )
    assert resumed.status_code == 200
    assert resumed.json()["ai_silent"] is False


def test_operator_header_is_required() -> None:
    """缺少操作人请求头时返回 401。"""
    client = _client()

    response = client.get("/v1/streams/st_1/state")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_unknown_stream_returns_404() -> None:
    """场次不存在时返回 404。"""
    client = _client()

    response = client.get("/v1/streams/st_missing/state", headers=HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_invalid_payload_returns_400() -> None:
    """非法动作返回统一参数错误。"""
    client = _client()

    response = client.post(
        "/v1/streams/st_1/control",
        json={"action": "explode"},
        headers=HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_audit_limit_is_validated_over_http() -> None:
    """审计条数越界返回参数错误。"""
    client = _client()

    response = client.get("/v1/streams/st_1/audit?limit=0", headers=HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_console_page_is_served() -> None:
    """控制台页面可直接访问，根路径跳转。"""
    client = _client()

    page = client.get(CONSOLE_PATH)
    assert page.status_code == 200
    assert "人工接管控制台" in page.text
    assert "/v1/streams/" in page.text

    root = client.get("/", follow_redirects=False)
    assert root.status_code in (301, 302, 307, 308)
    assert root.headers["location"] == CONSOLE_PATH


def test_healthz() -> None:
    """存活探针不依赖外部组件。"""
    client = _client()

    body: dict[str, Any] = client.get("/healthz").json()

    assert body == {"status": "ok"}
