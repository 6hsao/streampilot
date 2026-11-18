"""断流重连、指数退避与码率阶梯的测试（假控制端，不启动进程）。"""

import logging

import pytest

from streampilot.streamer import (
    BackoffPolicy,
    EncoderProfile,
    RecoveryState,
    StreamerError,
    StreamerRecovery,
    StreamTarget,
)

TARGET_A = StreamTarget(url="rtmp://a.example.com/live", key="key-node-a")
TARGET_B = StreamTarget(url="rtmp://b.example.com/live", key="key-node-b")


class _Clock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        """返回当前时间。"""
        return self.now


class _FakeControl:
    """记录调用的假推流控制端。"""

    def __init__(self, profile: EncoderProfile | None = None) -> None:
        self._profile = profile or EncoderProfile()
        self.start_error: StreamerError | None = None
        self.healthy = False
        self.starts: list[StreamTarget] = []
        self.stops = 0
        self.profiles: list[int] = []

    @property
    def profile(self) -> EncoderProfile:
        """当前编码参数。"""
        return self._profile

    def set_profile(self, profile: EncoderProfile) -> None:
        """记录新档位。"""
        self._profile = profile
        self.profiles.append(profile.bitrate_kbps)

    def start(self, target: StreamTarget) -> int:
        """启动推流；可配置为失败。"""
        self.starts.append(target)
        if self.start_error is not None:
            raise self.start_error
        self.healthy = True
        return 1000 + len(self.starts)

    def stop(self) -> None:
        """停止推流。"""
        self.stops += 1
        self.healthy = False

    def health(self) -> dict[str, object]:
        """返回健康快照。"""
        return {"state": "running" if self.healthy else "failed"}


def _recovery(
    control: _FakeControl,
    clock: _Clock,
    targets: list[StreamTarget] | None = None,
    *,
    stable_s: float = 60.0,
    failover_after: int = 3,
) -> StreamerRecovery:
    """构造重连器，缺省使用两个节点与手动时钟。"""
    return StreamerRecovery(
        control,
        targets or [TARGET_A, TARGET_B],
        clock=clock,
        stable_s=stable_s,
        failover_after=failover_after,
    )


def test_backoff_delay_sequence() -> None:
    """首次 1s，逐次翻倍，封顶 8s。"""
    policy = BackoffPolicy()

    delays = [policy.delay(attempt) for attempt in range(1, 6)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_validation() -> None:
    """非法退避参数与次数直接报错。"""
    with pytest.raises(ValueError, match="base_s"):
        BackoffPolicy(base_s=0)
    with pytest.raises(ValueError, match="factor"):
        BackoffPolicy(factor=0.5)
    with pytest.raises(ValueError, match="max_s"):
        BackoffPolicy(base_s=4, max_s=2)
    with pytest.raises(ValueError, match="attempt"):
        BackoffPolicy().delay(0)


def test_next_higher_respects_ceiling() -> None:
    """回补一档 400kbps，且不超过基准与全局上限。"""
    raised = EncoderProfile(bitrate_kbps=2100).next_higher()
    capped = EncoderProfile(bitrate_kbps=2400).next_higher(2500)

    assert raised is not None
    assert raised.bitrate_kbps == 2500
    assert capped is not None
    assert capped.bitrate_kbps == 2500
    assert EncoderProfile(bitrate_kbps=2500).next_higher(2500) is None
    assert EncoderProfile(bitrate_kbps=6000).next_higher() is None


def test_recovery_validation() -> None:
    """节点为空、阈值非法时拒绝构造。"""
    control = _FakeControl()

    with pytest.raises(ValueError, match="stream target"):
        StreamerRecovery(control, [])
    with pytest.raises(ValueError, match="failover_after"):
        _recovery(control, _Clock(), failover_after=0)
    with pytest.raises(ValueError, match="stable_s"):
        _recovery(control, _Clock(), stable_s=0)


def test_start_success_enters_running() -> None:
    """启动成功后进入 running 并应用当前档位。"""
    control = _FakeControl()
    recovery = _recovery(control, _Clock())

    recovery.start()

    assert recovery.state is RecoveryState.RUNNING
    assert recovery.failures == 0
    assert control.starts == [TARGET_A]
    assert control.profiles == [2500]


def test_start_failure_schedules_and_degrades() -> None:
    """首次启动失败：安排 1s 后重试并降一档。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)

    recovery.start()

    assert recovery.state is RecoveryState.RECONNECTING
    assert recovery.failures == 1
    assert recovery.profile.bitrate_kbps == 2100
    assert recovery.target is TARGET_A


def test_retry_waits_for_backoff_window() -> None:
    """退避未到期不重试，到期后重试一次。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    clock.now = 0.5
    recovery.tick()

    assert len(control.starts) == 1

    clock.now = 1.0
    recovery.tick()

    assert len(control.starts) == 2
    assert recovery.failures == 2
    assert recovery.state is RecoveryState.RECONNECTING


def test_failover_after_three_failures_on_same_node() -> None:
    """同节点连续 3 次失败后切到备用节点。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)

    recovery.start()
    for moment in (1.0, 3.0, 7.0):
        clock.now = moment
        recovery.tick()

    assert control.starts == [
        TARGET_A,
        TARGET_A,
        TARGET_A,
        TARGET_B,
    ]
    assert recovery.target is TARGET_B


def test_bitrate_stops_at_floor() -> None:
    """连续失败逐档下降，最低保持 800kbps。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)

    recovery.start()
    clock.now = 100.0
    for _ in range(6):
        recovery.tick()
        clock.now += 8.0

    assert recovery.profile.bitrate_kbps == 800
    assert control.profiles[-1] == 800


def test_promotes_after_stable_window() -> None:
    """恢复并稳定 60s 后回补一档，且以一次受控重启生效。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    control.start_error = None
    clock.now = 1.0
    recovery.tick()

    clock.now = 2.0
    recovery.tick()
    assert recovery.state is RecoveryState.RUNNING

    clock.now = 61.9
    recovery.tick()
    assert recovery.profile.bitrate_kbps == 2100

    clock.now = 62.0
    recovery.tick()

    assert recovery.profile.bitrate_kbps == 2500
    assert control.stops == 1
    assert len(control.starts) == 3


def test_promotion_restart_failure_reschedules() -> None:
    """回补时重启失败按失败处理、降档并重新安排。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    control.start_error = None
    clock.now = 1.0
    recovery.tick()

    clock.now = 2.0
    recovery.tick()

    control.start_error = StreamerError("boom")
    clock.now = 62.0
    recovery.tick()

    assert recovery.profile.bitrate_kbps == 2100
    assert recovery.state is RecoveryState.RECONNECTING
    assert recovery.failures == 1
    assert control.stops == 1


def test_process_exit_is_detected() -> None:
    """进程意外退出后进入重连并等待退避窗口。"""
    control = _FakeControl()
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    control.healthy = False
    clock.now = 10.0
    recovery.tick()

    assert recovery.state is RecoveryState.RECONNECTING
    assert recovery.failures == 1
    assert len(control.starts) == 1


def test_stable_at_baseline_does_not_restart() -> None:
    """已处于基准码率时稳定运行不触发重启。"""
    control = _FakeControl()
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    clock.now = 61.0
    recovery.tick()

    assert control.stops == 0
    assert len(control.starts) == 1


def test_single_target_does_not_rotate() -> None:
    """只有一个节点时连续失败也不切换。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock, targets=[TARGET_A])

    recovery.start()
    for moment in (1.0, 3.0, 7.0):
        clock.now = moment
        recovery.tick()

    assert recovery.target is TARGET_A
    assert all(target is TARGET_A for target in control.starts)


def test_stop_disables_reconnect() -> None:
    """停止后 tick 不再发起重连。"""
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)
    recovery.start()

    recovery.stop()
    clock.now = 100.0
    recovery.tick()

    assert recovery.state is RecoveryState.STOPPED
    assert len(control.starts) == 1
    assert control.stops == 1


def test_logs_avoid_stream_url_and_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重连日志只含序号与码率，不含地址与密钥。"""
    caplog.set_level(logging.WARNING)
    control = _FakeControl()
    control.start_error = StreamerError("boom")
    clock = _Clock()
    recovery = _recovery(control, clock)

    recovery.start()
    for moment in (1.0, 3.0, 7.0):
        clock.now = moment
        recovery.tick()

    text = caplog.text
    assert "rtmp://" not in text
    assert "key-node-a" not in text
    assert "key-node-b" not in text
