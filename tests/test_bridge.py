"""本地凭据桥的测试（假传输，不触网）。"""

import inspect
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from streampilot.bridge import client as client_module
from streampilot.bridge import runner as runner_module
from streampilot.bridge.client import (
    BridgeAuthError,
    BridgeClient,
    BridgeError,
    BridgeUnavailable,
)
from streampilot.bridge.models import (
    BridgeHeartbeat,
    RtmpCredential,
    format_iso,
)
from streampilot.bridge.runner import BridgeRunner
from streampilot.bridge.tokens import TokenManager

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _Clock:
    """可手动推进的单调时钟。"""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _tokens(clock: _Clock, *, ttl_s: float = 900.0) -> TokenManager:
    """构造每次刷新都递增的令牌管理。"""
    counter = {"n": 0}

    def fetch() -> str:
        counter["n"] += 1
        return f"tok-{counter['n']}"

    return TokenManager(fetch, ttl_s=ttl_s, clock=clock)


def _credential() -> RtmpCredential:
    return RtmpCredential(
        url="rtmp://push.example.com/live", key="streamkey-abc"
    )


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    tokens: TokenManager | None = None,
) -> BridgeClient:
    """用 MockTransport 构造客户端。"""
    transport = httpx.MockTransport(handler)
    return BridgeClient(
        "https://api.example.com",
        bridge_id="br_1",
        tokens=tokens or _tokens(_Clock()),
        client=httpx.Client(transport=transport),
    )


def test_credential_repr_is_masked() -> None:
    """凭据的 repr 不泄露密钥与查询参数。"""
    text = repr(_credential())

    assert "streamkey-abc" not in text
    assert "key='***'" in text


def test_credential_validation() -> None:
    """地址协议与密钥都要校验。"""
    with pytest.raises(ValueError):
        RtmpCredential(url="https://example.com", key="x")
    with pytest.raises(ValueError):
        RtmpCredential(url="rtmp://h/live", key="   ")


def test_heartbeat_payload_uses_utc() -> None:
    """无时区时间按 UTC 输出，秒级且带 Z。"""
    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="sha256:abc",
        companion_running=True,
        captured_at=datetime(2026, 1, 1, 20, 0),
    )

    payload = beat.as_payload()

    assert payload["captured_at"] == "2026-01-01T20:00:00Z"
    assert payload["version"] == "0.1.0"
    assert "key" not in payload
    assert format_iso(NOW) == "2026-01-01T12:00:00Z"


def test_token_manager_rotates_before_expiry() -> None:
    """接近过期时提前刷新，15 分钟内保持同一令牌。"""
    clock = _Clock()
    manager = _tokens(clock)

    first = manager.current()
    clock.value = 800.0
    assert manager.current() == first
    clock.value = 850.0
    second = manager.current()
    assert second != first

    manager.invalidate()
    assert manager.current() == "tok-3"


def test_token_manager_validates_options() -> None:
    """有效期与余量必须合法，空令牌报错。"""
    with pytest.raises(ValueError):
        TokenManager(lambda: "x", ttl_s=0)
    with pytest.raises(ValueError):
        TokenManager(lambda: "x", ttl_s=10, skew_s=10)
    with pytest.raises(ValueError):
        TokenManager(lambda: "  ").current()


def test_client_requires_https_and_identifier() -> None:
    """仅允许 https，桥标识不能为空。"""
    tokens = _tokens(_Clock())

    with pytest.raises(ValueError):
        BridgeClient("http://api.example.com", bridge_id="br", tokens=tokens)
    with pytest.raises(ValueError):
        BridgeClient("https://api.example.com", bridge_id=" ", tokens=tokens)


def test_heartbeat_posts_with_token() -> None:
    """心跳走约定路径并携带短期令牌。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="sha256:abc",
        companion_running=True,
        captured_at=NOW,
    )

    _client(handler).heartbeat(beat)

    request = seen[0]
    assert request.url.path == "/v1/bridge/heartbeat"
    assert request.headers["Authorization"] == "Bearer tok-1"
    body = json.loads(request.content)
    assert body["companion_running"] is True
    assert "streamkey-abc" not in request.content.decode()


def test_upload_rtmp_sends_key_once_and_parses_expiry() -> None:
    """凭据上报包含 key、一次性引用与过期时间。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"stored": True, "expires_at": "2026-01-01T20:00:00Z"},
        )

    expires = _client(handler).upload_rtmp(
        stream_id="st_1", credential=_credential()
    )

    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/bridge/rtmp"
    assert body["stream_id"] == "st_1"
    assert body["rtmp"]["key"] == "streamkey-abc"
    assert body["rtmp"]["key_ref"].startswith("up_")
    assert "expires_at" not in body
    assert expires == datetime(2026, 1, 1, 20, 0, tzinfo=UTC)


def test_upload_rejected_when_not_stored() -> None:
    """云端明确未存储时按失败处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stored": False})

    with pytest.raises(BridgeError, match="not stored"):
        _client(handler).upload_rtmp(
            stream_id="st_1", credential=_credential()
        )


def test_auth_failure_invalidates_token() -> None:
    """401 作废令牌，下次请求重新取令牌。"""
    statuses = {"code": 401}
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["Authorization"])
        return httpx.Response(statuses["code"], json={})

    clock = _Clock()
    client = _client(handler, _tokens(clock))
    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="fp",
        companion_running=False,
        captured_at=NOW,
    )

    with pytest.raises(BridgeAuthError):
        client.heartbeat(beat)

    statuses["code"] = 200
    client.heartbeat(beat)

    assert headers == ["Bearer tok-1", "Bearer tok-2"]


def test_server_error_and_bad_payload() -> None:
    """5xx 与非法 JSON 都归为桥错误。"""

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="fp",
        companion_running=False,
        captured_at=NOW,
    )

    with pytest.raises(BridgeError, match="500"):
        _client(failing).heartbeat(beat)
    with pytest.raises(BridgeError, match="invalid json"):
        _client(broken).heartbeat(beat)


def test_network_errors_are_mapped_and_redacted() -> None:
    """超时与连接错误映射为不可用，且信息不含密钥。"""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="fp",
        companion_running=False,
        captured_at=NOW,
    )

    with pytest.raises(BridgeUnavailable, match="timed out"):
        _client(boom).heartbeat(beat)
    with pytest.raises(BridgeUnavailable) as caught:
        _client(refused).heartbeat(beat)
    assert "streamkey-abc" not in str(caught.value)


def test_runner_alerts_once_then_recovers() -> None:
    """断连只告警一次，恢复后重新计时。"""
    alerts: list[str] = []
    healthy = {"ok": False}

    class _Flaky:
        def heartbeat(self, beat: BridgeHeartbeat) -> None:
            if not healthy["ok"]:
                raise BridgeUnavailable("down")

    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="fp",
        companion_running=True,
        captured_at=NOW,
    )
    runner = BridgeRunner(
        _Flaky(),
        beat_factory=lambda: beat,
        on_alert=lambda: alerts.append("lost"),
    )

    first = runner.beat_once()
    second = runner.beat_once()
    alerting_before = runner.alerting

    healthy["ok"] = True
    recovered = runner.beat_once()
    alerting_after = runner.alerting

    assert first is False
    assert second is False
    assert alerts == ["lost"]
    assert alerting_before is True
    assert recovered is True
    assert alerting_after is False
    assert (runner.beats_sent, runner.failures) == (1, 2)


def test_runner_validates_interval_and_stops() -> None:
    """间隔必须为正，停止信号已置位时不发心跳。"""
    beat = BridgeHeartbeat(
        bridge_id="br_1",
        host_fingerprint="fp",
        companion_running=True,
        captured_at=NOW,
    )

    class _Silent:
        def heartbeat(self, beat: BridgeHeartbeat) -> None:
            raise AssertionError("must not send")

    with pytest.raises(ValueError):
        BridgeRunner(_Silent(), beat_factory=lambda: beat, interval_s=0)

    runner = BridgeRunner(_Silent(), beat_factory=lambda: beat)
    stop = threading.Event()
    stop.set()
    runner.serve(stop)

    assert runner.beats_sent == 0


def test_bridge_has_no_streaming_capability() -> None:
    """桥端代码不包含推流进程调用（合规硬约束）。"""
    for module in (client_module, runner_module):
        source = inspect.getsource(module)
        assert "subprocess" not in source
        assert "ffmpeg" not in source.lower()
