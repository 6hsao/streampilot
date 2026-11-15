"""场次状态机的单元测试。"""

import pytest

from streampilot.orchestrator import (
    InvalidTransition,
    SessionState,
    SessionStateMachine,
)

State = SessionState


def test_happy_path() -> None:
    """待机 → 互动 → 发言 → 冷却 → 待机。"""
    machine = SessionStateMachine()

    machine.transition(State.INTERACTING, "comment")
    machine.transition(State.SPEAKING, "reply")
    machine.transition(State.COOLDOWN, "spoken")
    machine.transition(State.IDLE, "quiet")

    assert machine.state is State.IDLE
    assert len(machine.history) == 4


def test_illegal_transition_is_rejected() -> None:
    """待机不能直接发言。"""
    machine = SessionStateMachine()

    with pytest.raises(InvalidTransition) as excinfo:
        machine.transition(State.SPEAKING)

    assert excinfo.value.current is State.IDLE
    assert excinfo.value.target is State.SPEAKING
    assert machine.state is State.IDLE


def test_takeover_has_priority_while_speaking() -> None:
    """发言中可被人工接管。"""
    machine = SessionStateMachine(state=State.SPEAKING)

    machine.takeover("operator-1")

    assert machine.state is State.TAKEOVER
    assert machine.history[-1] == (State.TAKEOVER, "takeover:operator-1")


def test_circuit_can_trip_from_active_states() -> None:
    """互动与发言状态均可触发熔断。"""
    for state in (State.IDLE, State.INTERACTING, State.SPEAKING):
        machine = SessionStateMachine(state=state)

        machine.trip("safety-unavailable")

        assert machine.state is State.CIRCUIT_OPEN


def test_reset_only_from_circuit() -> None:
    """只有熔断态可由人工重置。"""
    machine = SessionStateMachine(state=State.CIRCUIT_OPEN)

    machine.reset("operator-1")
    assert machine.state is State.IDLE

    with pytest.raises(InvalidTransition):
        machine.reset("operator-1")


def test_ended_is_terminal() -> None:
    """收播后禁止任何迁移。"""
    machine = SessionStateMachine()

    machine.end()

    assert machine.state is State.ENDED
    with pytest.raises(InvalidTransition):
        machine.transition(State.IDLE)
    with pytest.raises(InvalidTransition):
        machine.takeover("operator-1")


def test_history_is_append_only_and_reasoned() -> None:
    """迁移历史记录原因，便于审计。"""
    machine = SessionStateMachine()

    machine.transition(State.INTERACTING, "comment")
    machine.transition(State.SPEAKING, "llm-reply")

    assert machine.history == [
        (State.INTERACTING, "comment"),
        (State.SPEAKING, "llm-reply"),
    ]
